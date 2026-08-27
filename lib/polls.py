from datetime import datetime, timezone

import discord

from lib import database as db

COLOR_MAIN = discord.Color(0xFFA733)
COLOR_MAIN_DARK = discord.Color(0xFF9811)
COLOR_ALT = discord.Color(0xFFDA44)
COLOR_ALT_DARK = discord.Color(0xFFD422)

DURATIONS = [
	("1 hour", 60 * 60),
	("6 hours", 6 * 60 * 60),
	("12 hours", 12 * 60 * 60),
	("24 hours", 24 * 60 * 60),
	("3 days", 3 * 24 * 60 * 60),
	("1 week", 7 * 24 * 60 * 60),
	("2 weeks", 14 * 24 * 60 * 60),
]

BAR_LENGTH = 12
MAX_OPTION_LENGTH = 30
MAX_QUESTION_LENGTH = 80

BAR_HALF = "<:bh:1542580757292261436>"
BAR_FILLED = {
	"left": "<:bf_l:1542577806062387240>",
	"mid": "<:bf_m:1542577807433932811>",
	"right": "<:bf_r:1542577808830898278>",
}
BAR_EMPTY = {
	"left": "<:be_l:1542581023739478176>",
	"mid": "<:be_m:1542581022338846820>",
	"right": "<:be_r:1542581020128448615>",
}

CHARS_PER_SQUARE = 2.6
LINE_WIDTH = round(BAR_LENGTH * CHARS_PER_SQUARE)

########## ======================================================================== ##########

def render_bar(share: float) -> str:
	"""Renders BAR_LENGTH segments, using capped emoji at each end."""
	# how much of the bar (in segment units, 0..BAR_LENGTH) is filled
	filled_units = round(share * BAR_LENGTH * 2) / 2
	segments = []
	for i in range(BAR_LENGTH):
		position = "left" if i == 0 else "right" if i == BAR_LENGTH - 1 else "mid"
		remaining = filled_units - i
		if remaining >= 1:
			segments.append(BAR_FILLED[position])
		elif remaining == 0.5:
			segments.append(BAR_HALF)
		else:
			segments.append(BAR_EMPTY[position])
	return "".join(segments)

def option_lines(poll_id: int, options: list[str]) -> str:
	counts = db.option_counts(poll_id, len(options))
	total = sum(counts)
	lines = []
	for option, count in zip(options, counts):
		share = count / total if total else 0
		bar = render_bar(share)
		percent = round(share * 100)
		vote_text = f"{count} vote{'s' if count != 1 else ''}"
		stats = f"{percent}% ({vote_text})"
		pad = max(LINE_WIDTH - len(option) - len(stats), 1)
		name_row = f"`{option}{' ' * pad}{stats}`"
		lines.append(f"{name_row}\n{bar}")
	return "\n\n".join(lines)

def status_line(poll_id: int, expires_at: str, closed: bool) -> str:
	total = db.total_votes(poll_id)
	status = f"{total} vote{'s' if total != 1 else ''}"
	if closed:
		status += " • Closed"
	else:
		expires_dt = datetime.fromisoformat(expires_at)
		status += f" • Closes {discord.utils.format_dt(expires_dt, style='R')}"
	return status

########## ======================================================================== ##########

async def get_channel_or_fetch(bot: discord.Client, channel_id: int):
	"""bot.get_channel is cache-only so fall back to an actual API call if it isn't found"""
	channel = bot.get_channel(channel_id)
	if channel is not None:
		return channel
	try:
		return await bot.fetch_channel(channel_id)
	except (discord.NotFound, discord.Forbidden):
		return None

async def close_poll(bot: discord.Client, poll_id: int):
	"""Marks a poll closed and redraws its message with buttons disabled."""
	channel_id, message_id, already_closed = db.get_poll_message_ref(poll_id)
	if already_closed:
		return
	db.set_closed(poll_id, True)

	channel = await get_channel_or_fetch(bot, channel_id)
	if channel is None:
		return
	try:
		message = await channel.fetch_message(message_id)
	except discord.NotFound:
		return

	view = PollView(poll_id, db.get_poll_options(poll_id), closed=True)
	await message.edit(view=view)

async def refresh_poll_message(bot: discord.Client, poll_id: int):
	"""Re-renders a poll's live message. Fails silently if the message is not found."""
	ref = db.get_poll_message_ref(poll_id)
	if ref is None:
		return
	channel_id, message_id, closed = ref
	channel = await get_channel_or_fetch(bot, channel_id)
	if channel is None:
		return
	try:
		message = await channel.fetch_message(message_id)
	except discord.NotFound:
		return
	await message.edit(view=PollView(poll_id, db.get_poll_options(poll_id), closed=bool(closed)))

async def rename_reply_thread(bot: discord.Client, poll_id: int):
	"""Keeps the replies thread's name in sync after the poll's question is edited."""
	_, thread_id, question = db.get_poll_reply_thread(poll_id)
	if thread_id is None:
		return
	thread = await get_channel_or_fetch(bot, thread_id)
	if thread is None:
		return
	try:
		await thread.edit(name=f"Replies: {question}"[:100])
	except discord.HTTPException:
		pass

########## ======================================================================== ##########

class VoteButton(discord.ui.Button):
	def __init__(self, poll_id: int, option_index: int, label: str):
		super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"bonfire_vote:{poll_id}:{option_index}")
		self.poll_id = poll_id
		self.option_index = option_index

	async def callback(self, interaction: discord.Interaction):
		if db.is_closed(self.poll_id):
			await interaction.response.send_message("This poll is closed and no longer accepting votes.", ephemeral=True)
			return
		voter_hash = db.hash_voter(interaction.user.id, self.poll_id)
		db.cast_vote(self.poll_id, voter_hash, self.option_index)
		await interaction.response.edit_message(view=PollView(self.poll_id, db.get_poll_options(self.poll_id)))

class ReplyModal(discord.ui.Modal, title="Reply anonymously"):
	message = discord.ui.TextInput(label="Your message", style=discord.TextStyle.paragraph, max_length=1000)

	def __init__(self, poll_id: int):
		super().__init__()
		self.poll_id = poll_id

	async def on_submit(self, interaction: discord.Interaction):
		if db.is_closed(self.poll_id):
			await interaction.response.send_message("This poll is closed and no longer accepting replies.", ephemeral=True)
			return
		_, _, supports_replies = db.get_poll_render_data(self.poll_id)
		if not supports_replies:
			await interaction.response.send_message("Replies aren't available for polls posted in a DM or group chat.", ephemeral=True)
			return

		channel_id, thread_id, question = db.get_poll_reply_thread(self.poll_id)

		thread = await get_channel_or_fetch(interaction.client, thread_id) if thread_id else None
		if thread is None:
			channel = await get_channel_or_fetch(interaction.client, channel_id)
			# standalone thread, not directly attached to the poll message (it looks ugly otherwise)
			thread = await channel.create_thread(name=f"Replies: {question}"[:100], type=discord.ChannelType.public_thread)
			db.set_thread_id(self.poll_id, thread.id)

		await thread.send(f"**User:** {self.message.value}")

		now_iso = datetime.now(timezone.utc).isoformat()
		db.add_reply(self.poll_id, self.message.value, now_iso)

		await interaction.response.defer()
		await refresh_poll_message(interaction.client, self.poll_id)

class ReplyButton(discord.ui.Button):
	def __init__(self, poll_id: int, closed: bool = False, supports_replies: bool = True):
		# disabled if the poll is closed OR it's in a DM/group chat, which have no thread type to reply into
		super().__init__(emoji="💬", label="Reply", style=discord.ButtonStyle.primary, custom_id=f"bonfire_reply:{poll_id}", disabled=closed or not supports_replies)
		self.poll_id = poll_id

	async def callback(self, interaction: discord.Interaction):
		await interaction.response.send_modal(ReplyModal(self.poll_id))

class EndPollButton(discord.ui.Button):
	def __init__(self, poll_id: int, closed: bool = False):
		label = "Poll ended" if closed else "End poll"
		emoji = None if closed else "🔒"
		super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.danger, custom_id=f"bonfire_end:{poll_id}", disabled=closed)
		self.poll_id = poll_id

	async def callback(self, interaction: discord.Interaction):
		creator_hash = db.get_poll_creator_hash(self.poll_id)
		if db.hash_voter(interaction.user.id, self.poll_id) != creator_hash:
			await interaction.response.send_message("Only this poll's creator can end it!", ephemeral=True)
			return
		await close_poll(interaction.client, self.poll_id)
		await interaction.response.send_message("Your poll has been ended.", ephemeral=True)

class PollView(discord.ui.LayoutView):
	"""Renders the poll with Components V2."""

	def __init__(self, poll_id: int, options: list[str], closed: bool = False):
		super().__init__(timeout=None)

		question, expires_at, supports_replies = db.get_poll_render_data(poll_id)

		heading = "###" if len(question) > 30 else "##"
		container = discord.ui.Container(accent_color=COLOR_MAIN_DARK if closed else COLOR_MAIN)
		container.add_item(discord.ui.TextDisplay(f"{heading} {question}"))
		container.add_item(discord.ui.Separator())
		container.add_item(discord.ui.TextDisplay(option_lines(poll_id, options)))
		container.add_item(discord.ui.Separator())
		container.add_item(discord.ui.TextDisplay(f"-# {status_line(poll_id, expires_at, closed)} • Votes are anonymous."))

		vote_row = discord.ui.ActionRow()
		for i, option in enumerate(options):
			button = VoteButton(poll_id, i, option)
			button.disabled = closed
			vote_row.add_item(button)
		container.add_item(vote_row)

		action_row = discord.ui.ActionRow()
		action_row.add_item(ReplyButton(poll_id, closed, bool(supports_replies)))
		action_row.add_item(EndPollButton(poll_id, closed))
		container.add_item(action_row)

		self.add_item(container)
