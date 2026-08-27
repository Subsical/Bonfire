import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lib import database as db
from lib import polls

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all(), help_command=None)

# lets holders of this role post a poll into any channel in this one guild, bypassing the normal
# "can only post where you could post yourself" check on /poll's channel parameter
TRUSTED_GUILD_ID = 1452532120994975828
TRUSTED_ROLE_ID = 1452832054541549578

########## ======================================================================== ##########

@bot.event
async def on_ready():
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
@app_commands.choices(duration=[app_commands.Choice(name=name, value=seconds) for name, seconds in polls.DURATIONS])
async def poll(
	interaction: discord.Interaction, question: app_commands.Range[str, 1, polls.MAX_QUESTION_LENGTH], duration: app_commands.Choice[int],
	option1: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH], option2: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH],
	channel: discord.TextChannel | None = None,
	option3: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None,
	option4: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None,
	option5: app_commands.Range[str, 1, polls.MAX_OPTION_LENGTH] | None = None
):
	"""Start an anonymous poll. Neither you nor any voter is ever identified.

	:param question: The poll question
	:param duration: How long the poll stays open before it auto-closes
	:param option1: First option
	:param option2: Second option
	:param channel: Channel to post the poll in 
	:param option3: Third option (optional)
	:param option4: Fourth option (optional)
	:param option5: Fifth option (optional)
	"""
	options = [o for o in (option1, option2, option3, option4, option5) if o]
	assert len(options) <= 5, "Polls can have at most 5 options."

	target_channel = channel or interaction.channel
	if channel is not None:
		is_trusted = (
			interaction.guild_id == TRUSTED_GUILD_ID
			and isinstance(interaction.user, discord.Member)
			and interaction.user.get_role(TRUSTED_ROLE_ID) is not None
		)
		if not is_trusted:
			assert isinstance(interaction.user, discord.Member) and channel.permissions_for(interaction.user).send_messages, \
				"You can't post a poll in a channel you can't post in yourself."

	await interaction.response.send_message("Your anonymous poll is being posted...", ephemeral=True)

	expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	poll_id = db.create_poll(target_channel.id, interaction.user.id, question, options, expires_at)

	view = polls.PollView(poll_id, options)
	poll_message = await target_channel.send(view=view)
	db.set_message_id(poll_id, poll_message.id)

########## ======================================================================== ##########

# lets me see/edit poll content and state directly without having to go into the db manually
# DONT WORRY everything is still anonymized, the user ids are hashed and this doesn't access votes anyway
class PollsGroup(app_commands.Group):
	async def interaction_check(self, interaction: discord.Interaction) -> bool:
		if not await bot.is_owner(interaction.user):
			await interaction.response.send_message("This command is owner-only.", ephemeral=True)
			return False
		return True

polls_group = PollsGroup(name="polls", description="Owner-only: inspect and edit poll data directly.")
bot.tree.add_command(polls_group)

async def poll_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
	"""Shows each poll's question alongside its ID, filtered live as you type."""
	choices = []
	for poll_id, question, closed, expires_at, channel_id, message_id in db.list_polls():
		label = f"#{poll_id} — {question}"
		if current.lower() in label.lower():
			choices.append(app_commands.Choice(name=label[:100], value=poll_id))
	return choices[:25]

@polls_group.command(name="list")
async def polls_list(interaction: discord.Interaction):
	"""List every poll in the database."""
	rows = db.list_polls()
	if not rows:
		await interaction.response.send_message("No polls in the database.", ephemeral=True)
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
	"""View full detail on one poll.

	:param poll_id: The poll to view
	"""
	row = db.get_poll(poll_id)
	assert row is not None, f"No poll with ID {poll_id}."
	_, message_id, channel_id, question, options_raw, thread_id, expires_at, closed = row
	options = options_raw.split("\x1f")

	lines = [
		f"**#{poll_id}: {question}**",
		f"Status: {'closed' if closed else 'open'}",
		f"Expires: {discord.utils.format_dt(datetime.fromisoformat(expires_at), style='f')}",
		f"Channel: <#{channel_id}> • Message: {message_id} • Thread: {thread_id or 'none yet'}",
		"",
		polls.option_lines(poll_id, options),
	]
	await interaction.response.send_message("\n".join(lines), ephemeral=True)

@polls_group.command(name="edit")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_edit(interaction: discord.Interaction, poll_id: int, question: str | None = None, option1: str | None = None, option2: str | None = None, option3: str | None = None, option4: str | None = None, option5: str | None = None, extend: int | None = None):
	"""Edit a poll's question, options, and/or expiry directly.

	:param poll_id: The poll to edit
	:param question: New question text
	:param option1: New option 1
	:param option2: New option 2
	:param option3: New option 3
	:param option4: New option 4
	:param option5: New option 5
	:param extend: Extend (or shorten with a negative number) the expiry by this many minutes
	"""
	row = db.get_poll(poll_id)
	assert row is not None, f"No poll with ID {poll_id}."
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

	if extend is not None:
		new_expires_at = (datetime.fromisoformat(expires_at) + timedelta(minutes=extend)).isoformat()
		db.set_expires_at(poll_id, new_expires_at)

	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} updated.", ephemeral=True)

@polls_group.command(name="close")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_close(interaction: discord.Interaction, poll_id: int):
	"""Force-close a poll.

	:param poll_id: The poll to close
	"""
	assert db.get_poll(poll_id) is not None, f"No poll with ID {poll_id}."
	await polls.close_poll(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} closed.", ephemeral=True)

@polls_group.command(name="reopen")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
@app_commands.choices(duration=[app_commands.Choice(name=name, value=seconds) for name, seconds in polls.DURATIONS])
async def polls_reopen(interaction: discord.Interaction, poll_id: int, duration: app_commands.Choice[int]):
	"""Reopen a closed poll with a fresh expiry.

	:param poll_id: The poll to reopen
	:param duration: New duration from now
	"""
	assert db.get_poll(poll_id) is not None, f"No poll with ID {poll_id}."
	new_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration.value)).isoformat()
	db.set_expires_at(poll_id, new_expires_at)
	db.set_closed(poll_id, False)
	await polls.refresh_poll_message(bot, poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} reopened.", ephemeral=True)

@polls_group.command(name="delete")
@app_commands.autocomplete(poll_id=poll_id_autocomplete)
async def polls_delete(interaction: discord.Interaction, poll_id: int):
	"""Delete a poll and its votes from the database. Does not delete the Discord message.

	:param poll_id: The poll to delete
	"""
	assert db.get_poll(poll_id) is not None, f"No poll with ID {poll_id}."
	db.delete_poll(poll_id)
	await interaction.response.send_message(f"Poll #{poll_id} deleted from the database. Its Discord message (if any) was left as-is.", ephemeral=True)

########## ======================================================================== ##########

bot.run(os.environ['DISCORD_TOKEN'])
