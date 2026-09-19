"""Rules that watch messages and act on them.

A rule is a condition tree and a list of actions, both stored as JSON. Conditions
nest into any/all/not groups so a single rule can say "a link, but not from a
trusted role" without needing several rules to express it.
"""
import asyncio
import json
import re
import time
import traceback
from datetime import timedelta

import discord

from lib import database as db

# Discord resolves a link into an embed after the message is posted, so a rule that
# asks about embeds can't be answered yet. We wait for the edit that adds them
# rather than sleeping a fixed amount, and give up after this long.
EMBED_WAIT = 4.0

MAX_ACTIONS = 10
MAX_DEPTH = 5

URL_PATTERN = re.compile(r"https?://\S+", re.I)
INVITE_PATTERN = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.I)

# rule_id -> {user_id: when it last fired}, so a cooldown is per person per rule
_cooldowns: dict[int, dict[int, float]] = {}

# conditions that can't be judged until Discord has had a go at embedding
EMBED_CONDITIONS = {"has_embed", "embed_count"}

####### =================================================================== #######

def _text_of(message: discord.Message) -> str:
	return message.content or ""

def _matches_text(kind: str, haystack: str, needle: str, case_sensitive: bool) -> bool:
	if not case_sensitive:
		haystack, needle = haystack.lower(), needle.lower()
	if kind == "contains":
		return needle in haystack
	if kind == "equals":
		return haystack.strip() == needle.strip()
	if kind == "starts_with":
		return haystack.lstrip().startswith(needle)
	if kind == "ends_with":
		return haystack.rstrip().endswith(needle)
	if kind == "regex":
		try:
			return re.search(needle, haystack, 0 if case_sensitive else re.I) is not None
		except re.error:
			# a broken pattern shouldn't take the whole rule down with it
			return False
	return False

def _check_one(node: dict, message: discord.Message) -> bool:
	kind = node.get("type")
	value = node.get("value")
	case_sensitive = bool(node.get("case_sensitive"))

	if kind in ("contains", "equals", "starts_with", "ends_with", "regex"):
		return _matches_text(kind, _text_of(message), str(value or ""), case_sensitive)

	if kind == "has_url":
		return URL_PATTERN.search(_text_of(message)) is not None
	if kind == "has_invite":
		return INVITE_PATTERN.search(_text_of(message)) is not None
	if kind == "has_attachment":
		return len(message.attachments) > 0
	if kind == "attachment_count":
		return _compare(len(message.attachments), node)
	if kind == "has_embed":
		return len(message.embeds) > 0
	if kind == "embed_count":
		return _compare(len(message.embeds), node)
	if kind == "has_mention":
		return _compare(_mention_count(message, node.get("value")), node, "count")
	if kind == "length":
		return _compare(len(_text_of(message)), node)

	if kind == "in_channel":
		return bool(_channel_ids(message) & _ids(value))
	if kind == "from_user":
		return message.author.id in _ids(value)
	if kind == "has_role":
		roles = getattr(message.author, "roles", [])
		return any(role.id in _ids(value) for role in roles)
	if kind == "is_bot":
		return message.author.bot

	# an unknown condition never matches, so a typo can't silently match everything
	return False

def _channel_ids(message: discord.Message) -> set[int]:
	"""A message in a thread counts as being in the channel the thread hangs off,
	and in that channel's category, so a rule scoped to #general covers its threads."""
	channel = message.channel
	ids = {channel.id}
	parent_id = getattr(channel, "parent_id", None)
	if parent_id:
		ids.add(parent_id)
	category_id = getattr(channel, "category_id", None)
	if category_id:
		ids.add(category_id)
	parent = getattr(channel, "parent", None)
	if parent is not None and getattr(parent, "category_id", None):
		ids.add(parent.category_id)
	return ids

def _mention_count(message: discord.Message, targets) -> int:
	"""How many mentions to count. With no targets that's every mention in the
	message; with targets it's only the ones that were asked for."""
	wanted = {str(item) for item in (targets or []) if str(item).strip()}
	if not wanted:
		return len(message.mentions) + len(message.role_mentions) + (1 if message.mention_everyone else 0)

	total = 0
	if "everyone" in wanted and message.mention_everyone:
		total += 1
	ids = {int(item) for item in wanted if item.isdigit()}
	if ids:
		total += sum(1 for user in message.mentions if user.id in ids)
		total += sum(1 for role in message.role_mentions if role.id in ids)
	return total

def _ids(value) -> set[int]:
	if isinstance(value, (list, tuple, set)):
		return {int(item) for item in value}
	try:
		return {int(value)}
	except (TypeError, ValueError):
		return set()

def _compare(actual: int, node: dict, key: str = "value") -> bool:
	operator = node.get("operator", "gte")
	try:
		target = int(node.get(key, 0))
	except (TypeError, ValueError):
		return False
	if operator == "gt":
		return actual > target
	if operator == "gte":
		return actual >= target
	if operator == "lt":
		return actual < target
	if operator == "lte":
		return actual <= target
	if operator == "eq":
		return actual == target
	return False

def evaluate(node: dict, message: discord.Message, depth: int = 0) -> bool:
	"""Walks the condition tree. Groups are {"type": "all"|"any"|"not", "children": [...]}."""
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return False
	kind = node.get("type")
	if kind in ("all", "any", "not"):
		children = node.get("children") or []
		if not children:
			# an empty group would otherwise match every message in the server
			return False
		if kind == "all":
			return all(evaluate(child, message, depth + 1) for child in children)
		if kind == "any":
			return any(evaluate(child, message, depth + 1) for child in children)
		# not() negates the whole group, so several children read as "none of these"
		return not all(evaluate(child, message, depth + 1) for child in children)
	return _check_one(node, message)

def needs_embeds(node: dict, depth: int = 0) -> bool:
	"""Whether anything in this tree asks about embeds, which arrive late."""
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return False
	if node.get("type") in EMBED_CONDITIONS:
		return True
	return any(needs_embeds(child, depth + 1) for child in node.get("children") or [])

####### =================================================================== #######

def _placeholders(template: str, message: discord.Message) -> str:
	guild = message.guild
	return (
		template
		.replace("{user}", message.author.mention)
		.replace("{username}", message.author.display_name)
		.replace("{channel}", message.channel.mention)
		.replace("{server}", guild.name if guild else "")
		.replace("{content}", _text_of(message))
	)

def on_cooldown(rule_id: int, user_id: int, cooldown: int) -> bool:
	if cooldown <= 0:
		return False
	last = _cooldowns.setdefault(rule_id, {}).get(user_id, 0)
	if time.monotonic() - last < cooldown:
		return True
	_cooldowns[rule_id][user_id] = time.monotonic()
	return False

def forget_rule(rule_id: int):
	"""Drop a deleted rule's cooldowns, so the dict doesn't grow forever."""
	_cooldowns.pop(rule_id, None)

async def run_action(action: dict, message: discord.Message) -> bool:
	"""Runs one action. Returns whether it sent something the user can see."""
	kind = action.get("type")
	value = action.get("value")
	member = message.author if isinstance(message.author, discord.Member) else None

	if kind == "react":
		for emoji in (value if isinstance(value, list) else [value])[:5]:
			try:
				await message.add_reaction(str(emoji))
			except (discord.HTTPException, TypeError):
				# a deleted or unavailable emoji shouldn't stop the other actions
				continue
		return False

	if kind == "delete":
		await message.delete()
		return False

	if kind == "reply":
		await message.reply(
			_placeholders(str(value or ""), message),
			mention_author=bool(action.get("mention", False)),
			suppress_embeds=bool(action.get("suppress_embeds", False)),
		)
		return True

	if kind == "send":
		await message.channel.send(_placeholders(str(value or ""), message))
		return True

	if kind == "thread":
		name = _placeholders(str(value or "Discussion"), message)[:100]
		thread = await message.create_thread(name=name)
		opener = action.get("message")
		if opener:
			await thread.send(_placeholders(str(opener), message))
		return True

	if member is None:
		# everything below acts on a member, which only exists in a guild
		return False

	if kind == "add_role":
		roles = [message.guild.get_role(role_id) for role_id in _ids(value)]
		await member.add_roles(*[role for role in roles if role], reason="Autoresponse")
		return False

	if kind == "remove_role":
		roles = [message.guild.get_role(role_id) for role_id in _ids(value)]
		await member.remove_roles(*[role for role in roles if role], reason="Autoresponse")
		return False

	if kind == "kick":
		await member.kick(reason=_placeholders(str(value or "Autoresponse"), message))
		return False

	if kind == "ban":
		await member.ban(
			reason=_placeholders(str(value or "Autoresponse"), message),
			delete_message_seconds=int(action.get("delete_seconds", 0)),
		)
		return False

	if kind == "timeout":
		# seconds; Discord caps a timeout at 28 days
		seconds = min(int(value or 60), 28 * 24 * 60 * 60)
		await member.timeout(timedelta(seconds=seconds), reason="Autoresponse")
		return False

	return False

####### =================================================================== #######

async def wait_for_embeds(bot: discord.Client, message: discord.Message) -> discord.Message:
	"""Discord edits the message once it has resolved a link, so wait for that edit
	rather than guessing how long it takes. Returns the message either way."""
	if message.embeds:
		return message
	if not URL_PATTERN.search(_text_of(message)):
		# nothing to embed, so nothing to wait for
		return message

	def is_ours(before: discord.Message, after: discord.Message) -> bool:
		return after.id == message.id and bool(after.embeds)

	try:
		_, after = await bot.wait_for("message_edit", check=is_ours, timeout=EMBED_WAIT)
		return after
	except asyncio.TimeoutError:
		# no embed ever arrived, which is itself an answer
		return message

async def handle(bot: discord.Client, message: discord.Message):
	"""Runs every rule in the guild against one message."""
	if message.guild is None or message.author.bot:
		return
	if not db.module_enabled(message.guild.id, "autoresponses"):
		return

	rules = db.guild_rules(message.guild.id)
	if not rules:
		return

	# Only wait for embeds if a rule actually asks about them, so the common case
	# stays instant.
	parsed = []
	wants_embeds = False
	for rule_id, name, _enabled, _priority, conditions, actions, cooldown in rules:
		try:
			tree = json.loads(conditions)
			steps = json.loads(actions)
		except json.JSONDecodeError:
			continue
		parsed.append((rule_id, name, tree, steps, cooldown))
		wants_embeds = wants_embeds or needs_embeds(tree)

	if wants_embeds:
		message = await wait_for_embeds(bot, message)

	replied = False
	for rule_id, name, tree, steps, cooldown in parsed:
		try:
			if not evaluate(tree, message):
				continue
			if on_cooldown(rule_id, message.author.id, cooldown):
				continue
			for action in steps[:MAX_ACTIONS]:
				if action.get("type") in ("reply", "send", "thread") and replied:
					# one rule already answered, so don't pile on
					continue
				try:
					if await run_action(action, message):
						replied = True
				except discord.Forbidden:
					# missing permissions for this action only
					continue
				except discord.NotFound:
					# the message went away, so nothing left to act on
					return
		except Exception:
			print(f"Autoresponse rule {rule_id} ({name}) failed:")
			traceback.print_exc()

####### =================================================================== #######

MAX_RULES = 50
MAX_NAME = 60
MAX_TEXT = 2000
MAX_CONDITIONS = 30

TEXT_CONDITIONS = {"contains", "equals", "starts_with", "ends_with", "regex"}
COUNT_CONDITIONS = {"attachment_count", "embed_count", "length"}
ID_CONDITIONS = {"in_channel", "from_user", "has_role"}
BARE_CONDITIONS = {"has_url", "has_invite", "has_attachment", "has_embed", "is_bot"}
OPERATORS = {"gt", "gte", "lt", "lte", "eq"}

TEXT_ACTIONS = {"reply", "send", "thread"}
ID_ACTIONS = {"add_role", "remove_role"}
BARE_ACTIONS = {"delete"}
REASON_ACTIONS = {"kick", "ban"}

def _clean_condition(node, depth: int = 0, budget: list | None = None) -> dict:
	"""Rebuilds a condition from scratch, keeping only what we recognise. Anything
	the browser sends that isn't in the allow-list simply doesn't survive."""
	if budget is None:
		budget = [MAX_CONDITIONS]
	if not isinstance(node, dict):
		raise ValueError("A condition has to be an object.")  # noqa: TRY004
	if depth > MAX_DEPTH:
		raise ValueError("Conditions are nested too deeply.")
	budget[0] -= 1
	if budget[0] < 0:
		raise ValueError(f"A rule can have at most {MAX_CONDITIONS} conditions.")

	kind = node.get("type")
	if kind in ("all", "any", "not"):
		children = node.get("children")
		if not isinstance(children, list) or not children:
			raise ValueError("A group needs at least one condition inside it.")
		return {"type": kind, "children": [_clean_condition(child, depth + 1, budget) for child in children]}

	if kind in TEXT_CONDITIONS:
		value = str(node.get("value") or "").strip()
		if not value:
			raise ValueError("A text condition needs something to look for.")
		if kind == "regex":
			try:
				re.compile(value)
			except re.error as error:
				raise ValueError(f"That regex doesn't compile: {error}") from error
		return {"type": kind, "value": value[:MAX_TEXT], "case_sensitive": bool(node.get("case_sensitive"))}

	if kind == "has_mention":
		operator = node.get("operator", "gte")
		if operator not in OPERATORS:
			raise ValueError("That comparison isn't one we know.")
		try:
			value = max(1, int(node.get("count", 1)))
		except (TypeError, ValueError) as error:
			raise ValueError("A mention count needs a number.") from error
		# "everyone" covers @everyone and @here, which Discord reports the same way
		targets = [
			str(item) for item in (node.get("value") or [])
			if str(item) == "everyone" or str(item).isdigit()
		]
		return {"type": kind, "operator": operator, "count": value, "value": targets[:50]}

	if kind in COUNT_CONDITIONS:
		operator = node.get("operator", "gte")
		if operator not in OPERATORS:
			raise ValueError("That comparison isn't one we know.")
		try:
			value = int(node.get("value", 0))
		except (TypeError, ValueError) as error:
			raise ValueError("A count condition needs a number.") from error
		return {"type": kind, "operator": operator, "value": max(0, value)}

	if kind in ID_CONDITIONS:
		ids = sorted(_ids(node.get("value")))
		if not ids:
			raise ValueError("Pick at least one channel, role or person.")
		return {"type": kind, "value": [str(item) for item in ids[:50]]}

	if kind in BARE_CONDITIONS:
		return {"type": kind}

	raise ValueError(f"Unknown condition: {kind}")

def _clean_action(action) -> dict:
	if not isinstance(action, dict):
		raise ValueError("An action has to be an object.")  # noqa: TRY004
	kind = action.get("type")

	if kind == "react":
		emoji = action.get("value")
		emoji = emoji if isinstance(emoji, list) else [emoji]
		cleaned = [str(item).strip()[:64] for item in emoji if str(item or "").strip()][:5]
		if not cleaned:
			raise ValueError("Pick at least one emoji to react with.")
		return {"type": "react", "value": cleaned}

	if kind in TEXT_ACTIONS:
		value = str(action.get("value") or "").strip()
		if not value:
			raise ValueError("That action needs some text.")
		cleaned = {"type": kind, "value": value[:MAX_TEXT]}
		if kind == "reply":
			cleaned["mention"] = bool(action.get("mention"))
			cleaned["suppress_embeds"] = bool(action.get("suppress_embeds"))
		if kind == "thread" and action.get("message"):
			cleaned["message"] = str(action["message"]).strip()[:MAX_TEXT]
		return cleaned

	if kind in ID_ACTIONS:
		ids = sorted(_ids(action.get("value")))
		if not ids:
			raise ValueError("Pick at least one role.")
		return {"type": kind, "value": [str(item) for item in ids[:10]]}

	if kind == "timeout":
		try:
			seconds = int(action.get("value", 60))
		except (TypeError, ValueError) as error:
			raise ValueError("A timeout needs a length in seconds.") from error
		return {"type": "timeout", "value": max(1, min(seconds, 28 * 24 * 60 * 60))}

	if kind in REASON_ACTIONS:
		cleaned = {"type": kind, "value": str(action.get("value") or "Autoresponse").strip()[:400]}
		if kind == "ban":
			try:
				cleaned["delete_seconds"] = max(0, min(int(action.get("delete_seconds", 0)), 7 * 24 * 60 * 60))
			except (TypeError, ValueError):
				cleaned["delete_seconds"] = 0
		return cleaned

	if kind in BARE_ACTIONS:
		return {"type": kind}

	raise ValueError(f"Unknown action: {kind}")

def validate(body: dict) -> dict:
	"""Checks a rule that came from the website. Raises ValueError with something
	worth showing the user."""
	name = str(body.get("name") or "").strip()
	if not name:
		raise ValueError("Give the autoresponse a name.")

	conditions = _clean_condition(body.get("conditions"))
	if conditions["type"] not in ("all", "any", "not"):
		# always hand the engine a group, so the shape is predictable
		conditions = {"type": "all", "children": [conditions]}

	raw_actions = body.get("actions")
	if not isinstance(raw_actions, list) or not raw_actions:
		raise ValueError("An autoresponse needs at least one action.")
	if len(raw_actions) > MAX_ACTIONS:
		raise ValueError(f"A rule can have at most {MAX_ACTIONS} actions.")
	actions = [_clean_action(action) for action in raw_actions]

	try:
		priority = int(body.get("priority", 0))
		cooldown = max(0, min(int(body.get("cooldown", 0)), 24 * 60 * 60))
	except (TypeError, ValueError) as error:
		raise ValueError("Priority and cooldown have to be numbers.") from error

	return {
		"name": name[:MAX_NAME],
		"conditions": conditions,
		"actions": actions,
		"priority": max(-100, min(priority, 100)),
		"cooldown": cooldown,
		"enabled": bool(body.get("enabled", True)),
	}
