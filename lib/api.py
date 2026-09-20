import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

import discord
from aiohttp import web

from lib import autoresponses
from lib import database as db
from lib import polls, reactionroles, reminders

MODULES = {
	"polls", "reaction-roles", "autoresponses", "games",
	"games:rps", "games:tictactoe", "games:connectfour", "games:battleship",
}

RATE_LIMIT = 20
RATE_WINDOW = 10
_writes: dict[int, deque] = defaultdict(deque)

def _too_fast(guild_id: int) -> bool:
	now = time.monotonic()
	recent = _writes[guild_id]
	while recent and now - recent[0] > RATE_WINDOW:
		recent.popleft()
	if len(recent) >= RATE_LIMIT:
		return True
	recent.append(now)
	return False

def _authorised(request: web.Request) -> bool:
	secret = os.environ.get("BOT_API_SECRET")
	return bool(secret) and hmac.compare_digest(request.headers.get("X-Bot-Secret", ""), secret)

async def get_modules(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	return web.json_response({"disabled": sorted(db.disabled_modules(guild_id))})

async def set_module(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	body = await request.json()
	module, enabled = body.get("module"), body.get("enabled")

	if module not in MODULES or not isinstance(enabled, bool):
		raise web.HTTPBadRequest()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")
	# the bot only knows about servers it's actually in
	if request.app["bot"].get_guild(guild_id) is None:
		raise web.HTTPNotFound()

	db.set_module(guild_id, module, enabled)
	return web.json_response({"module": module, "enabled": enabled})

async def get_info(request: web.Request):
	"""Counts that only exist on the live guild object, not in the database."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild = request.app["bot"].get_guild(int(request.match_info["guild_id"]))
	if guild is None:
		raise web.HTTPNotFound()
	return web.json_response({
		"members": guild.member_count,
		"categories": len(guild.categories),
		"text_channels": len(guild.text_channels),
		"voice_channels": len(guild.voice_channels),
		"roles": len(guild.roles) - 1,  # exclude @everyone
	})

async def get_choices(request: web.Request):
	"""Channels, categories and roles, so the website can offer real names to pick from."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild = request.app["bot"].get_guild(int(request.match_info["guild_id"]))
	if guild is None:
		raise web.HTTPNotFound()

	channels = []
	# categories first, each followed by its own channels, so the website can show
	# the same shape people already see in Discord
	for category, contents in guild.by_category():
		inside = [
			{
				"id": str(channel.id), "name": channel.name,
				"kind": "forum" if isinstance(channel, discord.ForumChannel) else "channel",
				"parent": str(category.id) if category else None,
			}
			for channel in contents
			if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
		]
		if category is not None:
			channels.append({"id": str(category.id), "name": category.name, "kind": "category", "parent": None})
		channels.extend(inside)

	# the bot can only hand out roles below its own top one, and never a managed one
	can_manage = guild.me is not None and guild.me.guild_permissions.manage_roles
	top = guild.me.top_role if can_manage else None
	roles = [
		{
			"id": str(role.id),
			"name": role.name,
			"colour": f"#{role.colour.value:06X}" if role.colour.value else None,
			"assignable": bool(top and role < top and not role.managed),
		}
		for role in reversed(guild.roles) if not role.is_default()
	]
	return web.json_response({"channels": channels, "roles": roles})

async def get_rules(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	rules = [
		{
			"id": rule_id, "name": name, "enabled": bool(enabled), "priority": priority,
			"conditions": json.loads(conditions), "cooldown": cooldown,
			"threads": autoresponses.thread_mode(json.loads(conditions)),
			**autoresponses.split_actions(json.loads(actions)),
		}
		for rule_id, name, enabled, priority, conditions, actions, cooldown in db.guild_rules(guild_id, only_enabled=False)
	]
	return web.json_response({"rules": rules})

async def save_rule(request: web.Request):
	"""Creates a rule, or replaces one when the body carries an id."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	if request.app["bot"].get_guild(guild_id) is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	body = await request.json()
	try:
		fields = autoresponses.validate(body)
	except ValueError as error:
		raise web.HTTPBadRequest(text=str(error)) from error

	rule_id = body.get("id")
	if rule_id is None:
		if db.count_rules(guild_id) >= autoresponses.MAX_RULES:
			raise web.HTTPBadRequest(text=f"A server can have at most {autoresponses.MAX_RULES} autoresponses.")
		rule_id = db.create_rule(
			guild_id, fields["name"], json.dumps(fields["conditions"]),
			json.dumps(autoresponses.pack_actions(fields["actions"], fields["otherwise"], fields["elifs"])),
			priority=fields["priority"], cooldown=fields["cooldown"],
		)
		autoresponses.forget_guild(guild_id)
	else:
		rule_id = int(rule_id)
		if db.get_rule(rule_id, guild_id) is None:
			raise web.HTTPNotFound()
		db.update_rule(
			rule_id, guild_id,
			name=fields["name"], conditions=json.dumps(fields["conditions"]),
			actions=json.dumps(autoresponses.pack_actions(fields["actions"], fields["otherwise"], fields["elifs"])),
			priority=fields["priority"],
			cooldown=fields["cooldown"], enabled=int(fields["enabled"]),
		)
		autoresponses.forget_guild(guild_id)
	return web.json_response({"id": rule_id})

async def toggle_rule(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	rule_id = int(request.match_info["rule_id"])
	if db.get_rule(rule_id, guild_id) is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	body = await request.json()
	db.update_rule(rule_id, guild_id, enabled=int(bool(body.get("enabled"))))
	autoresponses.forget_guild(guild_id)
	return web.json_response({"id": rule_id, "enabled": bool(body.get("enabled"))})

async def delete_rule(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	rule_id = int(request.match_info["rule_id"])
	if db.get_rule(rule_id, guild_id) is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	db.delete_rule(rule_id, guild_id)
	autoresponses.forget_rule(rule_id)
	autoresponses.forget_guild(guild_id)
	return web.json_response({"deleted": rule_id})

class FieldError(ValueError):
	"""A validation failure the website can point at a particular box."""

	def __init__(self, field: str, message: str):
		super().__init__(message)
		self.field = field

async def _reminder_target(bot, body: dict, user_id: int):
	"""Where a reminder should be sent, checking the user may actually post there.
	Returns (channel_id, guild_id) or raises."""
	if body.get("dm"):
		user = bot.get_user(user_id)
		if user is None:
			raise web.HTTPBadRequest(text=json.dumps({"detail": "I can't find your account.", "field": "channel_id"}), content_type="application/json")
		channel = await user.create_dm()
		# no guild means deliver() sends it straight to the DM
		return channel.id, None

	channel_id = body.get("channel_id")
	if not channel_id:
		raise web.HTTPBadRequest(text=json.dumps({"detail": "Pick a channel for the reminder.", "field": "channel_id"}), content_type="application/json")

	channel = bot.get_channel(int(channel_id))
	if channel is None or channel.guild is None:
		raise web.HTTPBadRequest(text=json.dumps({"detail": "I can't see that channel.", "field": "channel_id"}), content_type="application/json")

	member = channel.guild.get_member(user_id)
	if member is None:
		raise web.HTTPForbidden(text=json.dumps({"detail": "You aren't in that server.", "field": "channel_id"}), content_type="application/json")
	permissions = channel.permissions_for(member)
	if not (permissions.view_channel and permissions.send_messages):
		raise web.HTTPForbidden(text=json.dumps({"detail": "You don't have permission to post in that channel.", "field": "channel_id"}), content_type="application/json")
	if not channel.permissions_for(channel.guild.me).send_messages:
		raise web.HTTPBadRequest(text=json.dumps({"detail": "I can't post in that channel.", "field": "channel_id"}), content_type="application/json")
	return channel.id, channel.guild.id

def _reminder_fields(body: dict, user_id: int):
	"""Shared checks for creating and editing, so both reject the same things."""
	message = str(body.get("message") or "").strip()
	if not message:
		raise FieldError("message", "A reminder needs a message.")
	if len(message) > reminders.MAX_MESSAGE_LENGTH:
		raise FieldError("message", f"Keep it under {reminders.MAX_MESSAGE_LENGTH} characters.")

	try:
		remind_at = reminders.parse_when(str(body.get("when") or ""), user_id)
	except ValueError as error:
		raise FieldError("when", str(error)) from error
	try:
		pre_offsets = reminders.parse_pre_offsets(body.get("pre_reminders") or None)
	except ValueError as error:
		raise FieldError("pre_reminders", str(error)) from error

	now = datetime.now(timezone.utc)
	if remind_at <= now:
		raise FieldError("when", "That time has already passed.")
	if any(remind_at - timedelta(seconds=o) <= now for o in pre_offsets):
		raise FieldError("pre_reminders", "One of those early nudges would already be in the past.")

	repeat = str(body.get("repeat") or "none")
	if repeat not in reminders.REPEATS:
		raise FieldError("repeat", "That isn't a repeat option we know.")
	return message, remind_at, repeat, pre_offsets

MENTION_PATTERN = re.compile(r"<(@[!&]?|#)(\d+)>")
JUMP_PATTERN = re.compile(r"discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)")

CHANNEL_KINDS = (
	(discord.ForumChannel, "forum"),
	(discord.VoiceChannel, "voice"),
	(discord.StageChannel, "stage"),
	(discord.CategoryChannel, "category"),
	(discord.Thread, "thread"),
)

def _channel_entry(channel) -> dict | None:
	"""A channel mention, with what it takes to draw it the way Discord does."""
	if channel is None or not hasattr(channel, "name"):
		return None
	kind = next((label for cls, label in CHANNEL_KINDS if isinstance(channel, cls)), "channel")
	entry = {"name": channel.name, "kind": kind}
	# the guild is only worth naming when the link points out of the one being read
	guild = getattr(channel, "guild", None)
	if guild is not None:
		entry["guild"] = guild.name
		if guild.icon:
			entry["icon"] = guild.icon.replace(size=32, static_format="webp").url
	return entry

def _mention_names(bot, text: str) -> dict:
	"""Names for the mentions in a reminder, so the website can show them instead
	of a bare @ or #."""
	names = {}
	# a message link carries a channel id too, so it can be shown by name
	for raw in JUMP_PATTERN.findall(text or ""):
		names[raw] = _channel_entry(bot.get_channel(int(raw)))
	for kind, raw in MENTION_PATTERN.findall(text or ""):
		target_id = int(raw)
		if kind == "#":
			names[raw] = _channel_entry(bot.get_channel(target_id))
		elif kind == "@&":
			role = next((g.get_role(target_id) for g in bot.guilds if g.get_role(target_id)), None)
			names[raw] = {"name": role.name, "kind": "role"} if role else None
		else:
			user = bot.get_user(target_id)
			names[raw] = {"name": user.display_name, "kind": "user"} if user else None
	return {raw: entry for raw, entry in names.items() if entry}

async def get_timezone(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	return web.json_response({
		"timezone": db.get_timezone(user_id),
		"manual": db.timezone_is_manual(user_id),
	})

async def set_timezone(request: web.Request):
	"""The website sends this on sign in, and when someone picks one by hand."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	body = await request.json()
	name = str(body.get("timezone") or "").strip()
	manual = bool(body.get("manual"))
	if name and not reminders.valid_zone(name):
		raise web.HTTPBadRequest(text="That isn't a timezone I know.")
	stored = db.set_timezone(user_id, name, manual)
	# a hand-picked zone is never overwritten by the automatic one
	return web.json_response({"timezone": db.get_timezone(user_id), "changed": stored})

async def get_reminders(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	bot = request.app["bot"]
	rows = [
		{
			"id": reminder_id, "message": message, "remind_at": remind_at,
			"repeat": repeat, "pre_offsets": pre_offsets,
			"channel_id": str(channel_id), "guild_id": str(guild_id) if guild_id else None,
			"dm": guild_id is None,
			"mentions": _mention_names(bot, message),
		}
		for reminder_id, message, remind_at, repeat, pre_offsets, channel_id, guild_id in db.list_reminders(user_id)
	]
	return web.json_response({"reminders": rows})

async def save_reminder(request: web.Request):
	"""Creates a reminder, or replaces one when the body carries an id."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	body = await request.json()
	if _too_fast(user_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	try:
		message, remind_at, repeat, pre_offsets = _reminder_fields(body, user_id)
	except ValueError as error:
		raise web.HTTPBadRequest(
			text=json.dumps({"detail": str(error), "field": getattr(error, "field", None)}),
			content_type="application/json",
		) from error
	channel_id, guild_id = await _reminder_target(request.app["bot"], body, user_id)

	reminder_id = body.get("id")
	if reminder_id is None:
		if len(db.list_reminders(user_id)) >= reminders.MAX_PER_USER:
			raise web.HTTPBadRequest(text=f"You already have {reminders.MAX_PER_USER} reminders, delete one first.")
		reminder_id = db.create_reminder(
			user_id, channel_id, guild_id, message, remind_at.isoformat(), repeat, pre_offsets)
	else:
		reminder_id = int(reminder_id)
		if db.get_reminder(reminder_id, user_id) is None:
			raise web.HTTPNotFound()
		db.update_reminder(
			reminder_id, user_id, message=message, remind_at=remind_at.isoformat(),
			repeat=repeat, pre_offsets=pre_offsets, channel_id=channel_id, guild_id=guild_id)
	return web.json_response({"id": reminder_id})

async def delete_reminder(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	reminder_id = int(request.match_info["reminder_id"])
	if _too_fast(user_id):
		raise web.HTTPTooManyRequests(text="Slow down.")
	if not db.delete_reminder_for(reminder_id, user_id):
		raise web.HTTPNotFound()
	return web.json_response({"deleted": reminder_id})

async def get_writable_channels(request: web.Request):
	"""Channels this person can actually post in, for the reminder channel picker."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	user_id = int(request.match_info["user_id"])
	out = []
	for guild in request.app["bot"].guilds:
		member = guild.get_member(user_id)
		if member is None:
			continue
		for channel in guild.text_channels:
			permissions = channel.permissions_for(member)
			if permissions.view_channel and permissions.send_messages \
					and channel.permissions_for(guild.me).send_messages:
				out.append({"id": str(channel.id), "name": channel.name, "guild": guild.name})
	return web.json_response({"channels": out})

async def delete_poll(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	poll_id = int(request.match_info["poll_id"])

	# scoped to the guild, so a poll id from elsewhere can't be deleted through it
	if db.get_poll(poll_id, guild_id) is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	await polls.delete_poll(request.app["bot"], poll_id)
	return web.json_response({"deleted": poll_id})

async def get_panels(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	panels = []
	for row in db.guild_panels(guild_id):
		panel = reactionroles.panel_dict(row)
		panel["options"] = reactionroles.options_of(panel["id"])
		panel["guild_id"] = str(panel["guild_id"])
		panel["channel_id"] = str(panel["channel_id"])
		panel["message_id"] = str(panel["message_id"]) if panel["message_id"] else None
		for option in panel["options"]:
			option["role_id"] = str(option["role_id"])
		panels.append(panel)
	return web.json_response({"panels": panels})

async def save_panel(request: web.Request):
	"""Creates a panel, or replaces one when the body carries an id."""
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	bot = request.app["bot"]
	guild = bot.get_guild(guild_id)
	if guild is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	body = await request.json()
	panel_id = body.get("id")

	# what the panel has to carry depends on whether Bonfire can write the message
	channel = channel_id = message_id = None
	owned = True
	if panel_id is None:
		if db.count_panels(guild_id) >= reactionroles.MAX_PANELS:
			raise web.HTTPBadRequest(text=f"A server can have at most {reactionroles.MAX_PANELS} panels.")
		try:
			channel_id = int(body.get("channel_id"))
		except (TypeError, ValueError) as error:
			raise web.HTTPBadRequest(text="Pick a channel for the panel.") from error
		channel = guild.get_channel(channel_id)
		if channel is None:
			raise web.HTTPBadRequest(text="That channel isn't in this server.")

		if str(body.get("message_id") or "").strip():
			try:
				message_id = reactionroles.message_id_from(body["message_id"], guild_id)
			except ValueError as error:
				raise web.HTTPBadRequest(text=str(error)) from error
			if db.panel_by_message(message_id) is not None:
				raise web.HTTPBadRequest(text="That message already has a panel on it.")
			try:
				adopted = await channel.fetch_message(message_id)
			except (discord.NotFound, discord.Forbidden) as error:
				raise web.HTTPBadRequest(text="I can't find that message in that channel.") from error
			# a message Bonfire sent itself can still be edited, so it counts as its own
			owned = adopted.author.id == bot.user.id
	else:
		panel_id = int(panel_id)
		existing = reactionroles.get_panel(panel_id, guild_id)
		if existing is None:
			raise web.HTTPNotFound()
		owned = existing["owned"]

	try:
		fields = reactionroles.validate(body, owned)
	except ValueError as error:
		raise web.HTTPBadRequest(text=str(error)) from error
	# a panel on someone else's message can only ever be reactions
	if not owned and fields["style"] != "reaction":
		raise web.HTTPBadRequest(text="A panel on an existing message can only use reactions.")

	if body.get("id") is None:
		panel_id = db.create_panel(
			guild_id, channel_id, fields["title"], fields["style"], fields["mode"],
			limit_count=fields["limit"], content=fields["content"],
			embed=json.dumps(fields["embed"]) if fields["embed"] else None,
			owned=owned, message_id=message_id, per_row=fields["per_row"],
		)
	else:
		db.update_panel(
			panel_id, guild_id, title=fields["title"], content=fields["content"],
			embed=json.dumps(fields["embed"]) if fields["embed"] else None,
			style=fields["style"], mode=fields["mode"], limit_count=fields["limit"],
			per_row=fields["per_row"],
		)

	db.set_panel_options(panel_id, [
		(option["role_id"], option["emoji"], option["label"], option["description"])
		for option in fields["options"]
	])
	# the panel is saved either way, so a posting problem is reported, not raised
	try:
		await reactionroles.refresh_panel(bot, panel_id)
	except discord.HTTPException as error:
		raise web.HTTPBadGateway(text=f"Saved, but I couldn't post it: {error.text or error}") from error
	return web.json_response({"id": panel_id})

async def delete_panel(request: web.Request):
	if not _authorised(request):
		raise web.HTTPUnauthorized()
	guild_id = int(request.match_info["guild_id"])
	panel_id = int(request.match_info["panel_id"])
	if reactionroles.get_panel(panel_id, guild_id) is None:
		raise web.HTTPNotFound()
	if _too_fast(guild_id):
		raise web.HTTPTooManyRequests(text="Slow down.")

	await reactionroles.delete_panel(request.app["bot"], panel_id, guild_id)
	return web.json_response({"deleted": panel_id})

async def start(bot, port: int = 8081):
	app = web.Application()
	app["bot"] = bot
	app.add_routes([
		web.get("/guilds/{guild_id}/info", get_info),
		web.get("/guilds/{guild_id}/modules", get_modules),
		web.post("/guilds/{guild_id}/modules", set_module),
		web.delete("/guilds/{guild_id}/polls/{poll_id}", delete_poll),
		web.get("/guilds/{guild_id}/choices", get_choices),
		web.get("/guilds/{guild_id}/rules", get_rules),
		web.post("/guilds/{guild_id}/rules", save_rule),
		web.post("/guilds/{guild_id}/rules/{rule_id}/enabled", toggle_rule),
		web.delete("/guilds/{guild_id}/rules/{rule_id}", delete_rule),
		web.get("/guilds/{guild_id}/panels", get_panels),
		web.post("/guilds/{guild_id}/panels", save_panel),
		web.delete("/guilds/{guild_id}/panels/{panel_id}", delete_panel),
		web.get("/users/{user_id}/timezone", get_timezone),
		web.post("/users/{user_id}/timezone", set_timezone),
		web.get("/users/{user_id}/reminders", get_reminders),
		web.post("/users/{user_id}/reminders", save_reminder),
		web.delete("/users/{user_id}/reminders/{reminder_id}", delete_reminder),
		web.get("/users/{user_id}/channels", get_writable_channels),
	])
	runner = web.AppRunner(app)
	await runner.setup()
	await web.TCPSite(runner, "0.0.0.0", port).start()
	return runner
