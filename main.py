import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lib import database as db
from lib import polls

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all(), help_command=None)

########## ======================================================================== ##########

@bot.event
async def on_ready():
	await bot.tree.sync()
	for guild in bot.guilds:
		bot.tree.copy_global_to(guild=guild)
		await bot.tree.sync(guild=guild)

	# reattach persistent views
	for poll_id, options_raw, closed in db.all_poll_views_data():
		bot.add_view(polls.PollView(poll_id, options_raw.split("\x1f"), closed=bool(closed)))

	check_expired_polls.start()
	print("---OUTPUT----------\nBonfire is here.")

@bot.event
async def on_resumed():
	print("// resumed session")

@bot.event
async def on_guild_join(guild: discord.Guild):
	bot.tree.copy_global_to(guild=guild)
	await bot.tree.sync(guild=guild)

########## ======================================================================== ##########

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
	error_messages = {
		commands.MemberNotFound: "Couldn't find the user specified. Can only lookup by **user ID**, **mention**, **username#tag** or **username**.",
		commands.ChannelNotFound: "Couldn't find the channel specified. Can only lookup by **channel ID** or **mention**.",
		commands.RoleNotFound: "Couldn't find the role specified. Can only lookup by **role ID**, **mention** and **name**.",
		commands.MessageNotFound: "Couldn't find the message specified. Can only lookup by **chnl_id-msg_id** or **message link**.",
		commands.MissingRequiredArgument: "Missing required parameter.",
		commands.BadArgument: "Invalid parameter.",
		discord.Forbidden: "I don't have permission to do that.",
		AssertionError: str(error.original) if hasattr(error, "original") else ""
	}

	if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
		pass
	elif type(error) in error_messages:
		await ctx.send(error_messages[type(error)], delete_after=5)
	elif hasattr(error, "original") and type(error.original) in error_messages:
		await ctx.send(error_messages[type(error.original)], delete_after=5)
	else:
		raise error

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	if isinstance(error.original, AssertionError):
		await interaction.response.send_message(str(error.original), ephemeral=True)
	else:
		raise error

########## ======================================================================== ##########

@tasks.loop(minutes=1)
async def check_expired_polls():
	for poll_id in db.expired_poll_ids():
		await polls.close_poll(bot, poll_id)

########## ======================================================================== ##########

@bot.tree.command(name="poll")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(duration=[app_commands.Choice(name=name, value=seconds) for name, seconds in polls.DURATIONS])
async def poll(
	interaction: discord.Interaction, question: app_commands.Range[str, 1, polls.MAX_QUESTION_LENGTH], duration: app_commands.Choice[int],
	option1: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH], option2: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH],
	option3: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None,
	option4: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None,
	option5: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None
):
	"""Start an anonymous poll. Neither you nor any voter is ever identified.

	:param question: The poll question
	:param duration: How long the poll stays open before it auto-closes
	:param option1: First option
	:param option2: Second option
	:param option3: Third option (optional)
	:param option4: Fourth option (optional)
	:param option5: Fifth option (optional)
	"""
	options = [o for o in (option1, option2, option3, option4, option5) if o]
	assert len(options) <= 5, "Polls can have at most 5 options."

	await interaction.response.send_message("Your anonymous poll is being posted...", ephemeral=True)

	target_channel = interaction.channel
	supports_replies = isinstance(target_channel, discord.abc.GuildChannel)

	expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	poll_id = db.create_poll(target_channel.id, interaction.user.id, question, options, expires_at, supports_replies, interaction.guild_id)

	view = polls.PollView(poll_id, options)
	poll_message = await target_channel.send(view=view)
	db.set_message_id(poll_id, poll_message.id)

########## ======================================================================== ##########
class PollsGroup(app_commands.Group):
	async def interaction_check(self, interaction: discord.Interaction) -> bool:
		if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
			await interaction.response.send_message("Only the server owner or an administrator can use this.", ephemeral=True)
			return False
		is_owner = interaction.user.id == interaction.guild.owner_id
		is_admin = interaction.user.guild_permissions.administrator
		if not (is_owner or is_admin):
			await interaction.response.send_message("Only the server owner or an administrator can use this.", ephemeral=True)
			return False
		return True

polls_group = PollsGroup(
	name="polls", description="Manage this server's polls (admin only)",
	allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
	allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
	default_permissions=discord.Permissions(administrator=True),
)
bot.tree.add_command(polls_group)

async def poll_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
	"""Shows each poll's question alongside its ID, filtered live as you type. Only this server's polls."""
	if interaction.guild_id is None:
		return []
	choices = []
	for poll_id, question, closed, expires_at, channel_id, message_id in db.list_polls(interaction.guild_id):
		label = f"#{poll_id} — {question}"
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=poll_id))
	return choices[:25]

@polls_group.command(name="list")
async def polls_list(interaction: discord.Interaction):
	"""List this server's polls."""
	rows = db.list_polls(interaction.guild_id)
	if not rows:
		await interaction.response.send_message("This server has no polls yet.", ephemeral=True)
		return
	lines = []
	for poll_id, question, closed, expires_at, channel_id, message_id in rows:
		status = "closed" if closed else f"open, closes {discord.utils.format_dt(datetime.fromisoformat(expires_at), style='R')}"
		jump_url = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"
		lines.append(f"**#{poll_id}** — [{question}]({jump_url}) ({status})")
	await interaction.response.send_message("\n".join(lines), ephemeral=True)

@polls_group.command(name="view")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_view(interaction: discord.Interaction, poll_id: int):
	"""See a poll's full details and current results.

	:param poll_id: Which poll (start typing its name to search)
	"""
	row = db.get_poll(poll_id, interaction.guild_id)
	assert row is not None, "That poll doesn't exist in this server."
	_, message_id, channel_id, question, options_raw, thread_id, expires_at, closed = row
	options = options_raw.split("\x1f")

	jump_url = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"
	lines = [
		f"**#{poll_id}: {question}**",
		f"Status: {'closed' if closed else 'open'}",
		f"Closes: {discord.utils.format_dt(datetime.fromisoformat(expires_at), style='f')}",
		f"Message: {jump_url} • Replies thread: {f'<#{thread_id}>' if thread_id else 'none yet'}",
		"",
		polls.option_lines(poll_id, options),
	]
	await interaction.response.send_message("\n".join(lines), ephemeral=True)

@polls_group.command(name="edit")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_edit(interaction: discord.Interaction, poll_id: int, question: str | None = None, option1: str | None = None, option2: str | None = None, option3: str | None = None, option4: str | None = None, option5: str | None = None, extend_minutes: int | None = None):
	"""Change a poll's question, options, or how long it stays open.

	:param poll_id: Which poll (start typing its name to search)
	:param question: Replace the question with this
	:param option1: Replace option 1 with this
	:param option2: Replace option 2 with this
	:param option3: Replace option 3 with this
	:param option4: Replace option 4 with this
	:param option5: Replace option 5 with this
	:param extend_minutes: Add this many minutes to the poll's closing time (use a negative number to make it close sooner)
	"""
	row = db.get_poll(poll_id, interaction.guild_id)
	assert row is not None, "That poll doesn't exist in this server."
	_, _, _, _, options_raw, _, expires_at, _ = row

	if question is not None:
		db.set_question(poll_id, question)
		await polls.rename_reply_thread(bot, poll_id)

	options = options_raw.split("\x1f")
	new_options = [option1, option2, option3, option4, option5]
	for i, new_option in enumerate(new_options):
		if new_option is not None and i < len(options):
			options[i] = new_option
	if options != options_raw.split("\x1f"):
		db.set_options(poll_id, options)

	if extend_minutes is not None:
		new_expires_at = (datetime.fromisoformat(expires_at) + timedelta(minutes=extend_minutes)).isoformat()
		db.set_expires_at(poll_id, new_expires_at)

	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} updated.", ephemeral=True)

@polls_group.command(name="close")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_close(interaction: discord.Interaction, poll_id: int):
	"""Close a poll early, before its scheduled end time.

	:param poll_id: Which poll (start typing its name to search)
	"""
	assert db.get_poll(poll_id, interaction.guild_id) is not None, "That poll doesn't exist in this server."
	await polls.close_poll(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} closed.", ephemeral=True)

@polls_group.command(name="reopen")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
@app_commands.choices(duration=[app_commands.Choice(name=name, value=seconds) for name, seconds in polls.DURATIONS])
async def polls_reopen(interaction: discord.Interaction, poll_id: int, duration: app_commands.Choice[int]):
	"""Reopen a closed poll for voting again, with a new expiry time.

	:param poll_id: Which poll (start typing its name to search)
	:param duration: How much longer it should stay open, starting now
	"""
	assert db.get_poll(poll_id, interaction.guild_id) is not None, "That poll doesn't exist in this server."
	new_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	db.set_expires_at(poll_id, new_expires_at)
	db.set_closed(poll_id, False)
	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} reopened.", ephemeral=True)

########## ======================================================================== ##########

bot.run(os.environ['DISCORD_TOKEN'])
