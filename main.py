import asyncio
import hashlib
import io
import json
import os
import random
import re
import signal
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import available_timezones

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lib import *


class Bonfire(commands.Bot):
	api_runner = None

	async def close(self):
		"""Do cleanup before the bot shuts down."""
		await games.shutdown()
		if self.api_runner is not None:
			await self.api_runner.cleanup()
		await super().close()

bot = Bonfire(command_prefix='!', intents=discord.Intents.all(), help_command=None)

SYNC_HASH_FILE = "command_sync.hash"
DEV_GUILD = discord.Object(id=954760200777265162)

def command_definitions_hash() -> str:
	"""A hash of every command's current definition, so we only sync when something actually changed"""
	all_commands = bot.tree.get_commands() + bot.tree.get_commands(guild=DEV_GUILD)
	payloads = [cmd.to_dict(bot.tree) for cmd in sorted(all_commands, key=lambda c: c.name)]
	return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()

def read_last_sync_hash() -> str | None:
	if not os.path.exists(SYNC_HASH_FILE):
		return None
	with open(SYNC_HASH_FILE) as f:
		return f.read().strip()

def write_last_sync_hash(value: str):
	with open(SYNC_HASH_FILE, "w") as f:
		f.write(value)

def filter_guild_id(interaction: discord.Interaction) -> int | None:
	"""Which guild this interaction should be scoped to. (None means all guilds)"""
	if interaction.guild_id == DEV_GUILD.id:
		return None
	return interaction.guild_id

def jump_url(guild_id: int | None, channel_id: int, message_id: int) -> str:
	return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"

####### =================================================================== #######

@bot.event
async def on_ready():
	current_hash = command_definitions_hash()
	last_hash = await asyncio.to_thread(read_last_sync_hash)

	if current_hash != last_hash:
		await bot.tree.sync()
		for guild in bot.guilds:
			if guild.id == DEV_GUILD.id:
				continue
			bot.tree.clear_commands(guild=guild)
			await bot.tree.sync(guild=guild)
		await bot.tree.sync(guild=DEV_GUILD)
		await asyncio.to_thread(write_last_sync_hash, current_hash)
		print("Command definitions changed, synced with Discord.")

	bot.add_dynamic_items(polls.VoteButton, polls.ReplyButton, polls.EndPollButton, reactionroles.RoleButton, reactionroles.RoleSelect)

	db.sync_guilds([(g.id, g.name, g.icon.key if g.icon else None) for g in bot.guilds])

	if bot.api_runner is None:
		bot.api_runner = await api.start(bot)

	check_expired_polls.start()
	check_reminders.start()
	# only looks at panels still marked as someone else's, so it stops costing anything
	await reactionroles.reclaim_panels(bot)
	print("---OUTPUT----------\nBonfire is here.")

@bot.event
async def on_guild_join(guild: discord.Guild):
	db.add_guild(guild.id, guild.name, guild.icon.key if guild.icon else None)

@bot.event
async def on_guild_remove(guild: discord.Guild):
	db.remove_guild(guild.id)

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
	db.add_guild(after.id, after.name, after.icon.key if after.icon else None)

@bot.event
async def on_resumed():
	print("// resumed session")

@bot.event
async def on_message(message: discord.Message):
	# a rule that waits on embeds would otherwise hold commands up behind it
	asyncio.create_task(autoresponses.handle(bot, message))
	await bot.process_commands(message)

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
	await games.abandon(payload.message_id)

@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
	for message_id in payload.message_ids:
		await games.abandon(message_id)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
	await reactionroles.on_reaction(bot, payload, adding=True)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
	await reactionroles.on_reaction(bot, payload, adding=False)

####### =================================================================== #######

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
		await ctx.send(f"{theme.ERR} {error_messages[type(error)]}", delete_after=8)
	elif hasattr(error, "original") and type(error.original) in error_messages:
		await ctx.send(f"{theme.ERR} {error_messages[type(error.original)]}", delete_after=8)
	else:
		raise error

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	# cooldowns are raised before the command runs, so there's no .original on them
	if isinstance(error, app_commands.CommandOnCooldown):
		await interaction.response.send_message(f"{theme.ERR} Slow down! Try again in {error.retry_after:.1f}s.", ephemeral=True)
	elif isinstance(getattr(error, "original", None), AssertionError):
		await interaction.response.send_message(f"{theme.ERR} {error.original}", ephemeral=True)
	else:
		raise error

####### =================================================================== #######

@tasks.loop(minutes=1)
async def check_expired_polls():
	for poll_id in db.expired_poll_ids():
		await polls.close_poll(bot, poll_id)

####### =================================================================== #######

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
	assert db.module_enabled(interaction.guild_id, "polls"), "Polls are turned off in this server."
	assert interaction.guild_id is None or bot.get_guild(interaction.guild_id) is not None, "Bonfire needs to be added to this server for /poll to work here."

	target_channel = interaction.channel
	supports_replies = isinstance(target_channel, discord.abc.GuildChannel)

	expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	poll_id = db.create_poll(target_channel.id, interaction.user.id, question, options, expires_at, supports_replies, interaction.guild_id)
	view = polls.PollView(poll_id, options)

	if interaction.guild_id is not None:
		await interaction.response.send_message("Your anonymous poll is being posted...", ephemeral=True)
		poll_message = await target_channel.send(view=view, suppress_embeds=True)
	else:
		await interaction.response.send_message(view=view, suppress_embeds=True)
		poll_message = await interaction.original_response()

	db.set_message_id(poll_id, poll_message.id)

####### =================================================================== #######
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
	"""Shows each poll's question alongside its ID, filtered live as you type."""
	if interaction.guild_id is None:
		return []
	choices = []
	for poll_id, question, closed, expires_at, channel_id, message_id, guild_id in db.list_polls(filter_guild_id(interaction)):
		label = f"#{poll_id} - {question}"
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=poll_id))
	return choices[:25]

@polls_group.command(name="list")
async def polls_list(interaction: discord.Interaction):
	"""List this server's polls."""
	rows = db.list_polls(filter_guild_id(interaction))
	if not rows:
		await interaction.response.send_message("This server has no polls yet.", ephemeral=True)
		return
	lines = []
	for poll_id, question, closed, expires_at, channel_id, message_id, guild_id in rows:
		status = "closed" if closed else f"open, closes {discord.utils.format_dt(datetime.fromisoformat(expires_at), style='R')}"
		lines.append(f"**#{poll_id}** - [{question}]({jump_url(guild_id, channel_id, message_id)}) ({status})")
	await interaction.response.send_message("\n".join(lines), suppress_embeds=True, ephemeral=True)

@polls_group.command(name="view")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_view(interaction: discord.Interaction, poll_id: int):
	"""See a poll's full details and current results.

	:param poll_id: Which poll (start typing its name to search)
	"""
	row = db.get_poll(poll_id, filter_guild_id(interaction))
	assert row is not None, "That poll doesn't exist in this server."
	_, message_id, channel_id, guild_id, question, options_raw, thread_id, expires_at, closed = row
	options = options_raw.split("\x1f")

	lines = [
		f"**#{poll_id}: {question}**",
		f"Status: {'closed' if closed else 'open'}",
		f"Closes: {discord.utils.format_dt(datetime.fromisoformat(expires_at), style='f')}",
		f"Message: {jump_url(guild_id, channel_id, message_id)}",
		f"Replies thread: {f'<#{thread_id}>' if thread_id else 'none yet'}",
		"",
		polls.option_lines(poll_id, options),
	]
	await interaction.response.send_message("\n".join(lines), suppress_embeds=True, ephemeral=True)

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
	row = db.get_poll(poll_id, filter_guild_id(interaction))
	assert row is not None, "That poll doesn't exist in this server."
	_, _, _, _, _, options_raw, _, expires_at, _ = row

	if question is not None:
		db.set_question(poll_id, question)
		await polls.rename_reply_thread(bot, poll_id)

	options = options_raw.split("\x1f")
	for i, new_option in enumerate([option1, option2, option3, option4, option5]):
		if new_option is None:
			continue
		assert i <= len(options), f"Poll #{poll_id} only has {len(options)} options, so option{i + 1} would leave a gap."
		if i < len(options):
			options[i] = new_option
		else:
			options.append(new_option)
	if options != options_raw.split("\x1f"):
		db.set_options(poll_id, options)

	if extend_minutes is not None:
		new_expires_at = (datetime.fromisoformat(expires_at) + timedelta(minutes=extend_minutes)).isoformat()
		db.set_expires_at(poll_id, new_expires_at)

	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"{theme.SUC} Poll #{poll_id} updated.", ephemeral=True)

@polls_group.command(name="close")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_close(interaction: discord.Interaction, poll_id: int):
	"""Close a poll early, before its scheduled end time.

	:param poll_id: Which poll (start typing its name to search)
	"""
	assert db.get_poll(poll_id, filter_guild_id(interaction)) is not None, "That poll doesn't exist in this server."
	await polls.close_poll(bot, poll_id)
	await interaction.response.send_message(f"{theme.SUC} Poll #{poll_id} closed.", ephemeral=True)

@polls_group.command(name="reopen")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
@app_commands.choices(duration=[app_commands.Choice(name=name, value=seconds) for name, seconds in polls.DURATIONS])
async def polls_reopen(interaction: discord.Interaction, poll_id: int, duration: app_commands.Choice[int]):
	"""Reopen a closed poll for voting again, with a new expiry time.

	:param poll_id: Which poll (start typing its name to search)
	:param duration: How much longer it should stay open, starting now
	"""
	assert db.get_poll(poll_id, filter_guild_id(interaction)) is not None, "That poll doesn't exist in this server."
	new_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	db.set_expires_at(poll_id, new_expires_at)
	db.set_closed(poll_id, False)
	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"{theme.SUC} Poll #{poll_id} reopened.", ephemeral=True)

####### =================================================================== #######

@polls_group.command(name="delete")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_delete(interaction: discord.Interaction, poll_id: int):
	"""Delete a poll and its votes for good, and remove its message.

	:param poll_id: Which poll (start typing its name to search)
	"""
	assert db.get_poll(poll_id, filter_guild_id(interaction)) is not None, "That poll doesn't exist in this server."
	await polls.delete_poll(bot, poll_id)
	await interaction.response.send_message(f"{theme.SUC} Poll #{poll_id} deleted.", ephemeral=True)

@bot.tree.command(name="deletepoll", guild=DEV_GUILD)
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def deletepoll(interaction: discord.Interaction, poll_id: int):
	"""Delete a poll from the database. (owner only)

	:param poll_id: Which poll (type its name to search)
	"""
	assert await bot.is_owner(interaction.user), "Only the bot owner can use this."
	assert db.get_poll(poll_id, None) is not None, f"No poll with ID {poll_id}."
	db.delete_poll(poll_id)
	await interaction.response.send_message(f"{theme.SUC} Poll #{poll_id} deleted.", ephemeral=True)

####### =================================================================== #######

@bot.tree.command(name="togif")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(
	quality=[app_commands.Choice(name="High (bigger file)", value=100), app_commands.Choice(name="Good (default)", value=90), app_commands.Choice(name="Medium", value=70), app_commands.Choice(name="Low (smallest file)", value=50)],
	width=[app_commands.Choice(name="Original", value=0), app_commands.Choice(name="1280px", value=1280), app_commands.Choice(name="800px (default)", value=800), app_commands.Choice(name="480px", value=480), app_commands.Choice(name="320px", value=320)],
)
async def togif(
	interaction: discord.Interaction,
	file: discord.Attachment | None = None,
	link: str | None = None,
	quality: app_commands.Choice[int] | None = None,
	width: app_commands.Choice[int] | None = None,
	fps: app_commands.Range[int, 1, 50] | None = None,
	start: app_commands.Range[float, 0, 86400] | None = None,
	duration: app_commands.Range[float, 0.1, float(media.MAX_GIF_SECONDS)] | None = None,
	speed: app_commands.Range[float, 0.25, 5.0] | None = None,
	reverse: bool = False,
	loop: bool = True,
	spoiler: bool = False,
):
	"""Convert an image or video into a GIF.

	:param file: The image or video to convert
	:param link: A direct link to an image or video, instead of uploading one
	:param quality: How much detail to keep (lower means a smaller file)
	:param width: Width of the GIF, in pixels. Doesn't upscale past the original
	:param fps: Frames per second (defaults to match)
	:param start: Skip this many seconds into the video before converting
	:param duration: How many seconds of the video to convert
	:param speed: Playback speed (defaults to x1)
	:param reverse: Play the result backwards
	:param loop: Whether the GIF loops forever
	:param spoiler: Send the GIF as a spoiler
	"""
	assert file is not None or link is not None, "You need to provide a file or link to convert to GIF!"

	if file is not None:
		suffix = os.path.splitext(file.filename)[1].lower()
		assert suffix in media.MEDIA_EXTENSIONS, "That file isn't an image or video I can convert."
		assert file.size <= media.MAX_SOURCE_BYTES, f"That file is too big to convert (max {media.MAX_SOURCE_BYTES // (1024*1024)}MB)."

	assert not media.queue_full(), "Converting too many files right now, please try again in a minute."

	await interaction.response.defer()

	if file is not None:
		source_name, data = file.filename, await file.read()
	else:
		data, suffix, error = await media.download(link, media.MAX_SOURCE_BYTES)
		if data is None:
			await interaction.followup.send(f"{theme.ERR} {error}", ephemeral=True)
			return
		source_name = os.path.basename(urlparse(link).path) or "converted"

	try:
		gif, error = await media.to_gif(
			data, suffix, media.upload_limit(interaction),
			fps=fps, width=(width.value if width else None) or None,
			quality=quality.value if quality else 90,
			start=start, duration=duration, reverse=reverse, loop_forever=loop, speed=speed or 1.0,
		)
	except Exception:
		traceback.print_exc()
		gif, error = None, "Something went wrong while converting that file."

	if gif is None:
		await interaction.followup.send(f"{theme.ERR} {error}", ephemeral=True)
		return

	# sanitizing file name as plain ascii
	base = "".join(c for c in os.path.splitext(source_name)[0].lstrip(".") if c.isascii() and (c.isalnum() or c in "-_"))
	name = f"{base[:60] or 'converted'}.gif"

	view = discord.ui.LayoutView(timeout=None)
	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{name}", spoiler=spoiler)))
	container.add_item(discord.ui.TextDisplay(f"-# {len(gif) / (1024 * 1024):.2f}MB • {name}"))
	view.add_item(container)

	await interaction.followup.send(view=view, file=discord.File(io.BytesIO(gif), filename=name))

####### =================================================================== #######

@bot.tree.command(name="userinfo")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.checks.cooldown(1, 2.0, key=lambda i: i.user.id)
async def userinfo_command(interaction: discord.Interaction, user: discord.User | None = None):
	"""Look up someone's profile images, badges and account details.

	:param user: Who to look up (defaults to you)
	"""
	await interaction.response.defer()

	# the cached user object has no banner or accent colour, so always refetch
	target = await bot.fetch_user((user or interaction.user).id)
	member = interaction.guild.get_member(target.id) if interaction.guild else None

	badges = userinfo.user_badges(target)
	boost = userinfo.boost_badge(member) if member else None
	if boost:
		badges.append(boost)

	container = discord.ui.Container(accent_color=target.accent_color or theme.COLOR_MAIN)

	title = target.display_name if target.display_name == target.name else f"{target.display_name} ({target.name})"
	header = [discord.ui.TextDisplay(f"## [{title}](https://discord.com/users/{target.id})\n{target.mention}")]
	if badges:
		header.append(discord.ui.TextDisplay(f"\n# {' '.join(badges)}"))

	avatar = member.guild_avatar if member is not None and member.guild_avatar is not None else target.display_avatar
	container.add_item(discord.ui.Section(*header, accessory=discord.ui.Thumbnail(userinfo.full_size(avatar), description="Avatar")))

	container.add_item(discord.ui.Separator())
	lines = [f"**Created:** {discord.utils.format_dt(target.created_at, style='D')} ({discord.utils.format_dt(target.created_at, style='R')})"]
	if member is not None:
		if member.joined_at:
			lines.append(f"**Joined:** {discord.utils.format_dt(member.joined_at, style='D')} ({discord.utils.format_dt(member.joined_at, style='R')})")
		if member.premium_since:
			lines.append(f"**Boosting since:** {discord.utils.format_dt(member.premium_since, style='D')}")
	if target.accent_color:
		lines.append(f"**Accent:** `{target.accent_color!s}`")
	if member is not None:
		lines += userinfo.member_summary(member)

	details = discord.ui.TextDisplay("\n".join(lines))
	if target.avatar_decoration is not None:
		container.add_item(discord.ui.Section(details, accessory=discord.ui.Thumbnail(
			userinfo.full_size(target.avatar_decoration), description="Avatar decoration")))
	else:
		container.add_item(details)

	if member is not None:
		container.add_item(discord.ui.Separator())
		container.add_item(discord.ui.TextDisplay(f"**Roles**\n{userinfo.role_list(member)}"))
		container.add_item(discord.ui.TextDisplay(f"**Key permissions**\n-# {userinfo.key_permissions(member)}"))

	links = [("Avatar", target.display_avatar)]
	if member is not None and member.guild_avatar is not None:
		links.append(("Server avatar", member.guild_avatar))
	if target.avatar_decoration is not None:
		links.append(("Decoration", target.avatar_decoration))
	if target.banner is not None:
		links.append(("Banner", target.banner))

	container.add_item(discord.ui.Separator())

	assets = " • ".join(f"[{label}]({userinfo.full_size(asset)})" for label, asset in links)
	container.add_item(discord.ui.TextDisplay(f"-# {assets}"))

	# banner last and on its own, so it keeps its own wide aspect ratio
	if target.banner is not None:
		container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(userinfo.full_size(target.banner), description="Banner")))

	view = discord.ui.LayoutView(timeout=None)
	view.add_item(container)
	await interaction.followup.send(view=view, allowed_mentions=discord.abc.AllowedMentions.none())

####### =================================================================== #######

@bot.tree.command(name="wheel")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
async def wheel_command(interaction: discord.Interaction, options: str, nogif: bool = False):
	"""Spin a wheel to pick one of your options at random.

	:param options: The options to choose between, separated by commas
	:param nogif: Just pick a winner instantly, without the animation
	"""
	choices = wheel.parse_options(options)
	assert len(choices) >= 2, "Please provide at least two options, separated by commas."
	assert len(choices) <= wheel.MAX_OPTIONS, f"Wheels can have at most {wheel.MAX_OPTIONS} options."
	assert all(len(c) <= wheel.MAX_OPTION_LENGTH for c in choices), f"Please keep each option under {wheel.MAX_OPTION_LENGTH} characters."

	if nogif:
		await interaction.response.send_message(f"🎡 **{random.choice(choices)}**", allowed_mentions=discord.AllowedMentions.none())
		return

	assert not media.queue_full(), "Drawing too many wheels right now, please try again in a minute."

	await interaction.response.defer()

	try:
		gif, still, _, error = await wheel.spin(choices, media.upload_limit(interaction))
	except Exception:
		traceback.print_exc()
		gif, still, _, error = None, None, 0, "Something went wrong while spinning the wheel."

	if gif is None:
		await interaction.followup.send(f"{theme.ERR} {error}", ephemeral=True)
		return

	spinning = discord.ui.LayoutView(timeout=None)
	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://wheel.gif")))
	container.add_item(discord.ui.TextDisplay("-# Spinning..."))
	spinning.add_item(container)

	await interaction.followup.send(view=spinning, file=discord.File(io.BytesIO(gif), filename="wheel.gif"))
	await asyncio.sleep(wheel.total_seconds()+2)

	result = discord.ui.LayoutView(timeout=None)
	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://result.png")))
	result.add_item(container)

	try:
		await interaction.edit_original_response(
			view=result, attachments=[discord.File(io.BytesIO(still), filename="result.png")],
			allowed_mentions=discord.AllowedMentions.none())
	except (discord.NotFound, discord.Forbidden):
		pass

####### =================================================================== #######

@tasks.loop(seconds=5)
async def check_reminders():
	await reminders.check_due(bot)

@check_reminders.before_loop
async def before_check_reminders():
	await bot.wait_until_ready()

reminder_group = app_commands.Group(
	name="reminder", description="Set reminders for yourself",
	allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
	allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
)
bot.tree.add_command(reminder_group)

@reminder_group.command(name="add")
@app_commands.choices(repeat=[app_commands.Choice(name=r, value=r) for r in reminders.REPEATS])
async def reminder_add(
	interaction: discord.Interaction, when: str,
	message: app_commands.Range[str, 1, reminders.MAX_MESSAGE_LENGTH],
	channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread | None = None,
	dm: bool | None = None,
	repeat: app_commands.Choice[str] | None = None,
	pre_reminders: str | None = None,
):
	"""Set a reminder for yourself.

	:param when: A duration (`10m`, `1h30m`, `1w2d`), a date (`2026-10-16 17:30`), or a timestamp
	:param message: What to remind you about
	:param channel: Where to send it (defaults to here or DMs)
	:param dm: Send it to your DMs instead of a channel
	:param repeat: Whether it should repeat (daily, weekly, monthly, yearly)
	:param pre_reminders: List of reminders about the upcoming reminder separated by commas, like `1d, 1h`
	"""
	assert not (dm and channel is not None), "Pick a channel or your DMs, not both."
	assert not (channel is not None and interaction.guild is None), "Picking a channel only works inside a server."

	if dm:
		target = await interaction.user.create_dm()
	else:
		target = channel or interaction.channel
	assert target is not None, "I can't work out where to send this. Try picking a channel."

	if channel is not None:
		member = interaction.user if isinstance(interaction.user, discord.Member) else None
		assert member is not None and channel.guild == interaction.guild, "You can only pick a channel in this server."
		permissions = channel.permissions_for(member)
		assert permissions.view_channel and permissions.send_messages, "You don't have permission to post in that channel."
		assert channel.permissions_for(interaction.guild.me).send_messages, "I can't post in that channel."

	try:
		remind_at = reminders.parse_when(when, interaction.user.id)
		pre_offsets = reminders.parse_pre_offsets(pre_reminders)
	except ValueError as error:
		raise AssertionError(str(error)) from None

	now = datetime.now(timezone.utc)
	assert remind_at > now, "That time has already passed."
	assert not any(remind_at - timedelta(seconds=o) <= now for o in pre_offsets), \
		"One of those pre-reminders would already be in the past."

	count = len(db.list_reminders(interaction.user.id))
	assert count < reminders.MAX_PER_USER, f"You already have {reminders.MAX_PER_USER} reminders, delete one first."

	repeat_value = repeat.value if repeat else "none"
	guild_id = None if dm else interaction.guild_id
	_ = db.create_reminder(interaction.user.id, target.id, guild_id, message, remind_at.isoformat(), repeat_value, pre_offsets)

	lines = [f"### {theme.SUC} Reminder", message,
		f"\n**When:** <t:{int(remind_at.timestamp())}:F> (<t:{int(remind_at.timestamp())}:R>)"]
	if dm:
		lines.append("**Where:** your DMs")
	elif channel is not None:
		lines.append(f"**Where:** {target.mention}")
	if repeat_value != "none":
		lines.append(f"**Repeats:** {repeat_value}")
	if pre_offsets:
		lines.append(f"**Early nudges:** {', '.join(reminders.format_duration(o) for o in pre_offsets)}")
	# a date means nothing without knowing whose clock it was on
	if reminders.is_absolute(when) and not db.get_timezone(interaction.user.id):
		lines.append("-# Read as UTC. Set yours with `/reminder timezone`.")

	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.TextDisplay("\n".join(lines)))
	view = discord.ui.LayoutView(timeout=None)
	view.add_item(container)
	await interaction.response.send_message(view=view, ephemeral=True)

@reminder_group.command(name="list")
async def reminder_list(interaction: discord.Interaction):
	"""See the reminders you have coming up."""
	rows = db.list_reminders(interaction.user.id)
	assert rows, "You don't have any reminders set."

	lines = []
	for reminder_id, message, remind_at, repeat, pre_raw, channel_id, guild_id in rows:
		stamp = int(datetime.fromisoformat(remind_at).timestamp())
		extras = []
		if repeat != "none":
			extras.append(f"repeats {repeat}")
		if pre_raw:
			extras.append(", ".join(reminders.format_duration(int(o)) for o in pre_raw.split(",") if o) + " early")
		suffix = f" -# ({'; '.join(extras)})" if extras else ""
		lines.append(f"**#{reminder_id}** <t:{stamp}:R> in <#{channel_id}>\n{message[:120]}{suffix}")

	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.TextDisplay(f"### ⏰ Your reminders ({len(rows)})"))
	container.add_item(discord.ui.TextDisplay("\n\n".join(lines)[:3900]))
	view = discord.ui.LayoutView(timeout=None)
	view.add_item(container)
	await interaction.response.send_message(view=view, ephemeral=True)

# rounding can tip a value up into the next unit
_BIGGER = {"d": "w", "h": "d", "m": "h"}

def _due_in(remind_at: str) -> str:
	"""How far off a reminder is, for a label that can't carry a Discord timestamp."""
	left = (datetime.fromisoformat(remind_at) - datetime.now(timezone.utc)).total_seconds()
	if left < 0:
		return "due"
	if left < 60:
		return "in under a minute"
	# rounded inside the unit it reaches, so five hours isn't "in 4h"
	for unit, size, per in (("w", 604800, None), ("d", 86400, 7), ("h", 3600, 24), ("m", 60, 60)):
		if left >= size:
			count = round(left / size)
			return f"in {count}{unit}" if per is None or count < per else f"in 1{_BIGGER[unit]}"
	return "in under a minute"

async def timezone_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
	"""Matches against the system's timezone list as you type."""
	typed = current.replace(" ", "_").lower()
	zones = sorted(available_timezones())
	# the full list means nothing before you type, so start with the common ones
	if not typed:
		zones = [zone for zone in zones if zone in reminders.COMMON_ZONES]
	else:
		zones = [zone for zone in zones if typed in zone.lower()]
	return [app_commands.Choice(name=zone, value=zone) for zone in zones[:25]]

@reminder_group.command(name="timezone")
@app_commands.autocomplete(name=timezone_autocomplete)
async def reminder_timezone(interaction: discord.Interaction, name: str | None = None):
	"""Set the timezone your reminder times are read in.

	:param name: Somewhere like Europe/Paris (leave empty to see what yours currently is)
	"""
	if name is None:
		current = db.get_timezone(interaction.user.id)
		if not current:
			await interaction.response.send_message(
				"You haven't set a timezone, so times you type are read as UTC. "
				"Sign in to the website or use `/reminder timezone` to set one.", ephemeral=True)
			return
		now = datetime.now(reminders.zone_of(interaction.user.id))
		how = "set by you" if db.timezone_is_manual(interaction.user.id) else "from the website"
		await interaction.response.send_message(
			f"Your timezone is **{current}** ({how}), where it's currently {now.strftime('%H:%M')}.", ephemeral=True)
		return

	assert reminders.valid_zone(name), f"I don't know a timezone called `{name}`. Try something like `Europe/Berlin`."
	db.set_timezone(interaction.user.id, name, manual=True)
	now = datetime.now(reminders.zone_of(interaction.user.id))
	await interaction.response.send_message(
		f"{theme.SUC} Timezone set to **{name}**, where it's currently {now.strftime('%H:%M')}. "
		"Times you type from now on are read in that zone.", ephemeral=True)

async def reminder_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
	"""Shows each reminder's message and how soon it's due, filtered live as you type."""
	choices = []
	for reminder_id, message, remind_at, _repeat, _pre, _channel, _guild in db.list_reminders(interaction.user.id):
		label = f"#{reminder_id} - {_due_in(remind_at)} - {message}"
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=reminder_id))
	return choices[:25]

@reminder_group.command(name="edit")
@app_commands.choices(repeat=[app_commands.Choice(name=r, value=r) for r in reminders.REPEATS])
@app_commands.autocomplete(reminder_id=reminder_id_autocomplete)
async def reminder_edit(
	interaction: discord.Interaction, reminder_id: int,
	when: str | None = None,
	message: app_commands.Range[str, 1, reminders.MAX_MESSAGE_LENGTH] | None = None,
	channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread | None = None,
	dm: bool | None = None,
	repeat: app_commands.Choice[str] | None = None,
	pre_reminders: str | None = None,
):
	"""Change one of your reminders.

	:param reminder_id: See /reminder list to find the ID
	:param when: A duration (`10m`, `1h30m`, `1w2d`), a date (`2026-10-16 17:30`), or a timestamp
	:param message: What to remind you about
	:param channel: Where to send it
	:param dm: Send it to your DMs instead of a channel
	:param repeat: Whether it should repeat (daily, weekly, monthly, yearly)
	:param pre_reminders: List of reminders about the upcoming reminder separated by commas, like `1d, 1h`
	"""
	existing = db.get_reminder(reminder_id, interaction.user.id)
	assert existing is not None, "You don't have a reminder with that ID."
	assert not (dm and channel is not None), "Pick a channel or your DMs, not both."
	assert not (channel is not None and interaction.guild is None), "Picking a channel only works inside a server."

	changes = {}
	if message is not None:
		changes["message"] = message

	if when is not None:
		try:
			remind_at = reminders.parse_when(when, interaction.user.id)
		except ValueError as error:
			raise AssertionError(str(error)) from None
		assert remind_at > datetime.now(timezone.utc), "That time has already passed."
		changes["remind_at"] = remind_at.isoformat()

	if repeat is not None:
		changes["repeat"] = repeat.value

	if pre_reminders is not None:
		try:
			pre_offsets = reminders.parse_pre_offsets(pre_reminders)
		except ValueError as error:
			raise AssertionError(str(error)) from None
		# they only make sense against whichever time the reminder ends up with
		stamp = datetime.fromisoformat(changes.get("remind_at", existing[5]))
		assert not any(stamp - timedelta(seconds=o) <= datetime.now(timezone.utc) for o in pre_offsets), \
			"One of those pre-reminders would already be in the past."
		changes["pre_offsets"] = pre_offsets

	if dm:
		target = await interaction.user.create_dm()
		changes["channel_id"], changes["guild_id"] = target.id, None
	elif channel is not None:
		member = interaction.user if isinstance(interaction.user, discord.Member) else None
		assert member is not None and channel.guild == interaction.guild, "You can only pick a channel in this server."
		permissions = channel.permissions_for(member)
		assert permissions.view_channel and permissions.send_messages, "You don't have permission to post in that channel."
		assert channel.permissions_for(interaction.guild.me).send_messages, "I can't post in that channel."
		changes["channel_id"], changes["guild_id"] = channel.id, interaction.guild_id

	assert changes, "Tell me what to change."
	db.update_reminder(reminder_id, interaction.user.id, **changes)

	row = db.get_reminder(reminder_id, interaction.user.id)
	stamp = int(datetime.fromisoformat(row[5]).timestamp())
	lines = [f"### {theme.SUC} Reminder #{reminder_id} updated", row[4],
		f"\n**When:** <t:{stamp}:F> (<t:{stamp}:R>)",
		f"**Where:** <#{row[2]}>"]
	if row[6] != "none":
		lines.append(f"**Repeats:** {row[6]}")
	if row[7]:
		lines.append(f"**Early nudges:** {', '.join(reminders.format_duration(int(o)) for o in row[7].split(',') if o)}")

	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	container.add_item(discord.ui.TextDisplay("\n".join(lines)))
	view = discord.ui.LayoutView(timeout=None)
	view.add_item(container)
	await interaction.response.send_message(view=view, ephemeral=True)

@reminder_group.command(name="delete")
@app_commands.autocomplete(reminder_id=reminder_id_autocomplete)
async def reminder_delete(interaction: discord.Interaction, reminder_id: int):
	"""Delete one of your reminders.

	:param reminder_id: See /reminder list to find the ID
	"""
	assert db.get_reminder(reminder_id, interaction.user.id) is not None, "You don't have a reminder with that ID."
	db.delete_reminder(reminder_id)
	await interaction.response.send_message(f"{theme.SUC} Reminder #{reminder_id} deleted.", ephemeral=True)

####### =================================================================== #######

vs_group = app_commands.Group(
	name="vs", description="Play a game against someone",
	allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
	allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
)
bot.tree.add_command(vs_group)

async def start_game(interaction: discord.Interaction, key: str, title: str, build, opponent: discord.User | None):
	"""Posts a challenge. The opponent has to accept, if no opponent is specified then anyone can."""
	assert db.module_enabled(interaction.guild_id, f"games:{key}"), f"{title} is turned off in this server."
	assert opponent is None or not opponent.bot, "You can't play against a bot!"
	assert opponent is None or opponent.id != interaction.user.id, "You can't play against yourself!"
	assert not games.busy(interaction.user), "You're already in a game!"
	assert not games.busy(opponent), f"{opponent.display_name} is already in a game!" if opponent else ""

	challenge = games.ChallengeView(interaction.user, opponent, title, build)
	mentions = discord.AllowedMentions.none() if opponent is None else discord.AllowedMentions(
		everyone=False, roles=False, users=[opponent])
	await interaction.response.send_message(view=challenge, allowed_mentions=mentions)
	games.track(challenge, await interaction.original_response())

@vs_group.command(name="rps")
async def vs_rps(interaction: discord.Interaction, opponent: discord.User | None = None):
	"""Play rock paper scissors against someone.

	:param opponent: Play against a specific user
	"""
	await start_game(interaction, "rps", "✊ Rock Paper Scissors",
		lambda accepter: games.RPSView(interaction.user, accepter), opponent)

@vs_group.command(name="tictactoe")
async def vs_tictactoe(interaction: discord.Interaction, opponent: discord.User | None = None):
	"""Play TicTacToe against someone.

	:param opponent: Play against a specific user
	"""
	await start_game(interaction, "tictactoe", "⭕ Tic Tac Toe",
		lambda accepter: games.TicTacToeView(interaction.user, accepter), opponent)

@vs_group.command(name="connectfour")
async def vs_connectfour(interaction: discord.Interaction, opponent: discord.User | None = None):
	"""Play Connect Four against someone.

	:param opponent: Play against a specific user
	"""
	await start_game(interaction, "connectfour", "🧮 Connect Four",
		lambda accepter: games.ConnectFourView(interaction.user, accepter), opponent)

@vs_group.command(name="battleship")
async def vs_battleship(interaction: discord.Interaction, opponent: discord.User | None = None):
	"""Play Battleship against someone.

	:param opponent: Play against a specific user
	"""
	await start_game(interaction, "battleship", f"{games.BATTLESHIP} Battleship",
		lambda accepter: games.BattleshipView(interaction.user, accepter), opponent)

####### =========================== autoresponses ========================== #######

class AutoResponsesGroup(app_commands.Group):
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

autoresponse_group = AutoResponsesGroup(
	name="autoresponse", description="Manage this server's autoresponses (admin only)",
	allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
	allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
	default_permissions=discord.Permissions(administrator=True),
)
bot.tree.add_command(autoresponse_group)

async def rule_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
	if interaction.guild_id is None:
		return []
	choices = []
	for rule_id, name, enabled, *_ in db.guild_rules(interaction.guild_id, only_enabled=False):
		label = f"#{rule_id} - {name}" + ("" if enabled else " (off)")
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=rule_id))
	return choices[:25]

@autoresponse_group.command(name="list")
async def autoresponse_list(interaction: discord.Interaction):
	"""List all the autoresponses in this server."""
	rules = db.guild_rules(interaction.guild_id, only_enabled=False)
	if not rules:
		await interaction.response.send_message("No autoresponses set up yet.", ephemeral=True)
		return

	lines = []
	for rule_id, name, enabled, priority, conditions, actions, cooldown in rules:
		try:
			branches = autoresponses.split_actions(json.loads(actions))
			kinds = [step.get("type", "?") for step in branches["actions"]]
			for branch in branches["elifs"]:
				kinds += [f"else if {step.get('type', '?')}" for step in branch.get("actions") or []]
			kinds += [f"else {step.get('type', '?')}" for step in branches["otherwise"]]
		except json.JSONDecodeError:
			kinds = ["(unreadable)"]
		state = "on" if enabled else "off"
		lines.append(f"`#{rule_id}` **{name}** - {state}, priority {priority} - {', '.join(kinds)}")

	embed = discord.Embed(title="Autoresponses", description="\n".join(lines)[:4000], color=theme.COLOR_MAIN)
	await interaction.response.send_message(embed=embed, ephemeral=True)

@autoresponse_group.command(name="toggle")
@app_commands.autocomplete(rule=rule_id_autocomplete)
async def autoresponse_toggle(interaction: discord.Interaction, rule: int, enabled: bool):
	"""Toggle an autoresponse on or off.
	
	:param rule: Which autoresponse (start typing its name to search)
	:param enabled: Whether it should run
	"""
	existing = db.get_rule(rule, interaction.guild_id)
	assert existing, "That autoresponse doesn't exist in this server."
	db.update_rule(rule, interaction.guild_id, enabled=int(enabled))
	await interaction.response.send_message(
		f"{theme.SUC} **{existing[1]}** is now {'on' if enabled else 'off'}.", ephemeral=True
	)

@autoresponse_group.command(name="delete")
@app_commands.autocomplete(rule=rule_id_autocomplete)
async def autoresponse_delete(interaction: discord.Interaction, rule: int):
	"""Delete an autoresponse from this server.
	
	:param rule: Which autoresponse (start typing its name to search)
	"""
	existing = db.get_rule(rule, interaction.guild_id)
	assert existing, "That autoresponse doesn't exist in this server."
	db.delete_rule(rule, interaction.guild_id)
	autoresponses.forget_rule(rule)
	await interaction.response.send_message(f"{theme.SUC} Deleted **{existing[1]}**.", ephemeral=True)

@autoresponse_group.command(name="show")
@app_commands.autocomplete(rule=rule_id_autocomplete)
async def autoresponse_show(interaction: discord.Interaction, rule: int):
	"""Show the conditions and actions of an autoresponse.
	
	:param rule: Which autoresponse (start typing its name to search)
	"""
	existing = db.get_rule(rule, interaction.guild_id)
	assert existing, "That autoresponse doesn't exist in this server."
	rule_id, name, enabled, priority, conditions, actions, cooldown = existing

	embed = discord.Embed(title=name, color=theme.COLOR_MAIN)
	embed.add_field(name="Conditions", value=f"```json\n{conditions[:1000]}\n```", inline=False)
	embed.add_field(name="Actions", value=f"```json\n{actions[:1000]}\n```", inline=False)
	embed.set_footer(text=f"#{rule_id} - {'on' if enabled else 'off'} - priority {priority} - cooldown {cooldown}s")
	await interaction.response.send_message(embed=embed, ephemeral=True)

####### ========================== reaction roles ========================== #######

class ReactionRolesGroup(app_commands.Group):
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

reactionroles_group = ReactionRolesGroup(
	name="reactionroles", description="Manage this server's role panels (admin only)",
	allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
	allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
	default_permissions=discord.Permissions(administrator=True),
)
bot.tree.add_command(reactionroles_group)

async def panel_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
	if interaction.guild_id is None:
		return []
	choices = []
	for row in db.guild_panels(interaction.guild_id):
		panel = reactionroles.panel_dict(row)
		label = f"#{panel['id']} - {panel['title']}"
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=panel["id"]))
	return choices[:25]

@reactionroles_group.command(name="create", description="Start a new role panel")
@app_commands.choices(
	style=[app_commands.Choice(name="Reactions", value="reaction"), app_commands.Choice(name="Buttons", value="button"), app_commands.Choice(name="Dropdown", value="select")],
	mode=[app_commands.Choice(name="No limit", value="multiple"), app_commands.Choice(name="Only one", value="single"), app_commands.Choice(name="Limited to", value="limited")],
)
async def reactionroles_create(
	interaction: discord.Interaction, title: app_commands.Range[str, 1, reactionroles.MAX_TITLE],
	channel: discord.TextChannel | None = None,
	message: str | None = None,
	style: app_commands.Choice[str] | None = None,
	mode: app_commands.Choice[str] | None = None,
	limit: app_commands.Range[int, 1, 24] | None = None,
	description: app_commands.Range[str, 1, reactionroles.MAX_CONTENT] | None = None,
	per_row: app_commands.Range[int, 1, reactionroles.MAX_PER_ROW] | None = None,
):
	"""Create a new role panel.
	
	:param title: The heading shown on the panel
	:param channel: Where to post it (leave empty to use an existing message)
	:param message: Link or ID of a message to put reactions on instead
	:param style: How people pick their roles
	:param mode: How many roles someone can have
	:param limit: How many roles, when the mode is limited
	:param description: Text shown under the title
	:param per_row: How many buttons sit on each row (button panels, defaults to 4)
	"""
	assert db.module_enabled(interaction.guild_id, "reaction-roles"), "Reaction roles are turned off in this server."
	assert db.count_panels(interaction.guild_id) < reactionroles.MAX_PANELS, f"This server already has {reactionroles.MAX_PANELS} panels, delete one first."

	style_value = style.value if style else "reaction"
	mode_value = mode.value if mode else "multiple"
	assert mode_value != "limited" or limit, "Pick a limit when the mode is limited."

	existing = None
	if message:
		assert channel is None, "Pick a channel or an existing message, not both."
		existing = await find_message(interaction, message)
		style_value = "reaction"  # only reactions can be added to a message we didn't send

	target = existing.channel if existing else (channel or interaction.channel)
	assert target is not None, "I can't work out where to put this. Try picking a channel."
	permissions = target.permissions_for(interaction.guild.me)
	assert permissions.send_messages and permissions.add_reactions, f"I can't post or react in {target.mention}."

	panel_id = db.create_panel(
		interaction.guild_id, target.id, title, style_value, mode_value,
		limit_count=limit or 0, content=description or "",
		owned=existing is None, message_id=existing.id if existing else None,
		per_row=per_row or 0,
	)

	lines = [f"### {theme.SUC} Panel #{panel_id}", f"**{title}**"]
	lines.append(f"**Where:** {existing.jump_url if existing else target.mention}")
	lines.append(f"**Style:** {style_value}")
	lines.append(f"**Picks:** {reactionroles.mode_line({'mode': mode_value, 'limit': limit or 0})}")
	lines.append(f"\nAdd roles with `/reactionroles addrole panel:#{panel_id}`, or set it up on the website.")
	await interaction.response.send_message("\n".join(lines), ephemeral=True)

async def find_message(interaction: discord.Interaction, raw: str) -> discord.Message:
	"""A message from a jump link or a bare ID, somewhere in this server."""
	raw = raw.strip()
	link = re.search(r"/channels/(\d+)/(\d+)/(\d+)", raw)
	if link:
		guild_id, channel_id, message_id = (int(part) for part in link.groups())
		assert guild_id == interaction.guild_id, "That message is in another server."
	else:
		assert raw.isdigit(), "That doesn't look like a message link or ID."
		channel_id, message_id = interaction.channel_id, int(raw)

	channel = interaction.guild.get_channel_or_thread(channel_id)
	assert channel is not None, "I can't see the channel that message is in."
	try:
		found = await channel.fetch_message(message_id)
	except (discord.NotFound, discord.Forbidden) as error:
		raise AssertionError("I can't find that message.") from error
	assert db.panel_by_message(found.id) is None, "That message already has a panel on it."
	return found

@reactionroles_group.command(name="addrole")
@app_commands.autocomplete(panel=panel_id_autocomplete)
async def reactionroles_addrole(interaction: discord.Interaction, panel: int, role: discord.Role, emoji: str | None = None, label: app_commands.Range[str, 1, reactionroles.MAX_LABEL] | None = None):
	"""Add a role to a panel.
	
	:param panel: Which panel to add it to
	:param role: The role to hand out
	:param emoji: The emoji people react with (for reaction panels)
	:param label: What the button or dropdown row says (for button or dropdown panels)
	"""
	found = reactionroles.get_panel(panel, interaction.guild_id)
	assert found, "That panel doesn't exist in this server."
	cap = found["per_row"] * reactionroles.ROWS if found["style"] == "button" else reactionroles.MAX_OPTIONS
	assert len(reactionroles.options_of(panel)) < cap, f"A {found['style']} panel like this one fits {cap} roles."
	assert reactionroles.assignable(interaction.guild, role.id), f"I can't hand out {role.mention}. It's either above me in the role list or managed by an integration."

	if found["style"] == "reaction":
		assert emoji, "A reaction panel needs an emoji for every role."
	elif found["style"] == "select":
		assert label, "A dropdown panel needs a label for every role."
	else:
		assert emoji or label, "A button panel needs a label or an emoji for every role."

	if emoji:
		assert reactionroles.usable_emoji(emoji), "That doesn't look like an emoji I can use."
		taken = {reactionroles.emoji_key(option["emoji"]) for option in reactionroles.options_of(panel)}
		assert reactionroles.emoji_key(emoji) not in taken, "Another role on this panel already uses that emoji."

	added = db.add_panel_option(panel, role.id, emoji, label or role.name)
	assert added, f"{role.mention} is already on that panel."

	await interaction.response.defer(ephemeral=True)
	await reactionroles.refresh_panel(bot, panel)
	await interaction.followup.send(f"{theme.SUC} Added {role.mention} to **{found['title']}**.", ephemeral=True)

@reactionroles_group.command(name="removerole")
@app_commands.autocomplete(panel=panel_id_autocomplete)
async def reactionroles_removerole(interaction: discord.Interaction, panel: int, role: discord.Role):
	"""Take a role off a panel.
	
	:param panel: Which panel to remove it from
	:param role: The role to take off
	"""
	found = reactionroles.get_panel(panel, interaction.guild_id)
	assert found, "That panel doesn't exist in this server."
	assert db.remove_panel_option(panel, role.id), f"{role.mention} isn't on that panel."

	await interaction.response.defer(ephemeral=True)
	await reactionroles.refresh_panel(bot, panel)
	await interaction.followup.send(f"{theme.SUC} Took {role.mention} off **{found['title']}**.", ephemeral=True)

@reactionroles_group.command(name="post")
@app_commands.autocomplete(panel=panel_id_autocomplete)
async def reactionroles_post(interaction: discord.Interaction, panel: int):
	"""Post or refresh a panel's message

	:param panel: Which panel to post/update
	"""
	found = reactionroles.get_panel(panel, interaction.guild_id)
	assert found, "That panel doesn't exist in this server."
	assert reactionroles.options_of(panel), "Put at least one role on the panel first."

	await interaction.response.defer(ephemeral=True)
	# someone else's message is only ever given reactions, never rewritten
	assert found["owned"], "That panel is on a message I didn't send, so I can't post it."
	message = await reactionroles.post_panel(bot, found)
	assert message, "I couldn't post there. Check I can still see the channel."
	await interaction.followup.send(f"{theme.SUC} [Panel posted.]({message.jump_url})", ephemeral=True)

@reactionroles_group.command(name="list")
async def reactionroles_list(interaction: discord.Interaction):
	"""List all the role panels in this server."""
	rows = db.guild_panels(interaction.guild_id)
	if not rows:
		await interaction.response.send_message("No role panels set up yet.", ephemeral=True)
		return

	lines = []
	for row in rows:
		panel = reactionroles.panel_dict(row)
		count = len(reactionroles.options_of(panel["id"]))
		where = jump_url(panel["guild_id"], panel["channel_id"], panel["message_id"]) if panel["message_id"] else None
		title = f"[{panel['title']}]({where})" if where else panel["title"]
		lines.append(f"`#{panel['id']}` **{title}** - {panel['style']}, {count} role{'s' if count != 1 else ''}")

	embed = discord.Embed(title="Role panels", description="\n".join(lines)[:4000], color=theme.COLOR_MAIN)
	await interaction.response.send_message(embed=embed, ephemeral=True)

@reactionroles_group.command(name="delete")
@app_commands.autocomplete(panel=panel_id_autocomplete)
async def reactionroles_delete(interaction: discord.Interaction, panel: int):
	"""Delete a role panel.

	:param panel: Which panel to delete
	"""
	found = reactionroles.get_panel(panel, interaction.guild_id)
	assert found, "That panel doesn't exist in this server."
	await interaction.response.defer(ephemeral=True)
	await reactionroles.delete_panel(bot, panel, interaction.guild_id)
	await interaction.followup.send(f"{theme.SUC} Deleted **{found['title']}**.", ephemeral=True)

####### =================================================================== #######

def terminate(signum, frame):
	raise KeyboardInterrupt
signal.signal(signal.SIGTERM, terminate)

bot.run(os.environ['DISCORD_TOKEN'])
