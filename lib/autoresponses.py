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

# guild_id -> (stamp, parsed rules, whether any of them ask about embeds)
_parsed: dict[int, tuple] = {}

# conditions that can't be judged until Discord has had a go at embedding
EMBED_CONDITIONS = {"has_embed", "embed_count"}

SPEAKING_ACTIONS = {"reply", "send", "thread"}

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

THREAD_MODES = ("exclude", "only", "both")

def thread_mode(tree: dict) -> str:
	"""How a rule treats threads."""
	mode = tree.get("threads") if isinstance(tree, dict) else None
	return mode if mode in THREAD_MODES else "exclude"

def _is_thread(message: discord.Message) -> bool:
	return isinstance(message.channel, discord.Thread)

def _thread_ok(mode: str, message: discord.Message) -> bool:
	"""Whether a rule set to this thread mode should look at this message at all."""
	if mode == "only":
		return _is_thread(message)
	if mode == "both":
		return True
	# excluded by default, so a rule on a channel doesn't follow its threads
	return not _is_thread(message)

def _channel_ids(message: discord.Message) -> set[int]:
	"""Every channel a message counts as being in, including a thread's parent."""
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
	"""How many mentions to count."""
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

def _assignable(guild: discord.Guild, role_ids: set[int]) -> list[discord.Role]:
	"""The roles the bot is actually allowed to hand out, of the ones asked for."""
	if guild.me is None or not guild.me.guild_permissions.manage_roles:
		return []
	top = guild.me.top_role
	roles = (guild.get_role(role_id) for role_id in role_ids)
	return [role for role in roles if role and role < top and not role.managed]

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

def in_scope(node: dict, message: discord.Message, depth: int = 0) -> bool:
	"""Whether a message is somewhere the rule was pointed at, ignoring its other conditions."""
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return False
	kind = node.get("type")
	if kind == "in_channel":
		return _check_one(node, message)
	if kind == "not":
		children = node.get("children") or []
		if len(children) == 1 and children[0].get("type") == "in_channel":
			return not _check_one(children[0], message)
	if kind in ("all", "any", "not"):
		scopes = [child for child in node.get("children") or [] if _mentions_channel(child)]
		if not scopes:
			return True
		return all(in_scope(child, message, depth + 1) for child in scopes)
	# a rule with no channel condition runs everywhere, so it's always in scope
	return True

def _mentions_channel(node: dict, depth: int = 0) -> bool:
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return False
	if node.get("type") == "in_channel":
		return True
	return any(_mentions_channel(child, depth + 1) for child in node.get("children") or [])

def needs_embeds(node: dict, depth: int = 0) -> bool:
	"""Whether anything in this tree asks about embeds, which arrive late."""
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return False
	if node.get("type") in EMBED_CONDITIONS:
		return True
	return any(needs_embeds(child, depth + 1) for child in node.get("children") or [])

####### =================================================================== #######

VARIABLE_PATTERN = re.compile(r"\{\{ *([a-z]+)(?:\.([^{}]+?))? *\}\}")
USER_PING_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_PING_PATTERN = re.compile(r"<@&(\d+)>")

def _arguments(message: discord.Message, trigger: str | None) -> list[str]:
	"""What follows the trigger, so `!hello there you` can use `{{args.1}}`."""
	text = _text_of(message).strip()
	if trigger and text.lower().startswith(trigger.lower()):
		text = text[len(trigger):]
	return text.split()

def _by_name(items, name: str):
	"""Finds a role or channel by name, ignoring case."""
	wanted = name.strip().casefold()
	return next((item for item in items if item.name.casefold() == wanted), None)

def _variables(message: discord.Message, trigger: str | None) -> dict:
	author = message.author
	guild = message.guild
	arguments = _arguments(message, trigger)
	return {
		("user", "ping"): author.mention,
		("user", "mention"): author.mention,
		("user", "username"): author.name,
		("user", "displayname"): author.display_name,
		("user", "id"): str(author.id),
		("channel", "name"): getattr(message.channel, "name", ""),
		("channel", "ping"): getattr(message.channel, "mention", ""),
		("channel", "mention"): getattr(message.channel, "mention", ""),
		("server", "name"): guild.name if guild else "",
		("server", "members"): str(guild.member_count) if guild else "",
		("message", "content"): _text_of(message),
		("message", "link"): message.jump_url,
		("everyone", ""): "@everyone",
		("here", ""): "@here",
		("args", "all"): " ".join(arguments),
		**{("args", str(i + 1)): value for i, value in enumerate(arguments)},
	}

def find_trigger(node: dict, depth: int = 0) -> str | None:
	"""The 'starts with' text a rule matched on, so arguments can be read after it."""
	if not isinstance(node, dict) or depth > MAX_DEPTH:
		return None
	if node.get("type") == "starts_with":
		return str(node.get("value") or "") or None
	for child in node.get("children") or []:
		found = find_trigger(child, depth + 1)
		if found:
			return found
	return None

def _placeholders(template: str, message: discord.Message, trigger: str | None = None) -> str:
	values = _variables(message, trigger)

	guild = message.guild

	def swap(match: re.Match) -> str:
		kind, name = match.group(1), (match.group(2) or "").strip()
		key = (kind, name)
		if key in values:
			return values[key]

		if kind == "role" and guild:
			role = guild.get_role(int(name)) if name.isdigit() else _by_name(guild.roles, name)
			return role.mention if role else match.group(0)
		if kind == "channel" and guild and name:
			channel = guild.get_channel(int(name)) if name.isdigit() else _by_name(guild.channels, name)
			return channel.mention if channel else match.group(0)
		return "" if key[0] == "args" else match.group(0)

	return VARIABLE_PATTERN.sub(swap, template)

def _permitted_mentions(message: discord.Message, *templates: str) -> discord.AllowedMentions:
	"""What a response is allowed to ping, judged based on the template."""
	guild = message.guild
	everyone = False
	roles: list[discord.abc.Snowflake] = []
	users: list[discord.abc.Snowflake] = []
	for template in templates:
		users.extend(discord.Object(id=int(found)) for found in USER_PING_PATTERN.findall(template or ""))
		roles.extend(discord.Object(id=int(found)) for found in ROLE_PING_PATTERN.findall(template or ""))
		for match in VARIABLE_PATTERN.finditer(template or ""):
			kind, name = match.group(1), (match.group(2) or "").strip()
			if kind in ("everyone", "here"):
				everyone = True
			elif kind == "role" and guild and name:
				role = guild.get_role(int(name)) if name.isdigit() else _by_name(guild.roles, name)
				if role:
					roles.append(role)
			elif kind == "user" and name in ("ping", "mention"):
				users.append(message.author)
	return discord.AllowedMentions(everyone=everyone, roles=roles, users=users, replied_user=False)

def _build_embed(spec, message: discord.Message, trigger: str | None):
	"""Turns a saved embed into a real one, with variables filled in."""
	if not isinstance(spec, dict) or not spec:
		return None

	def text(key, limit):
		return _placeholders(str(spec.get(key) or ""), message, trigger)[:limit] or None

	embed = discord.Embed(
		title=text("title", 256),
		description=text("description", 4096),
		url=text("url", 2048),
	)
	if spec.get("colour"):
		try:
			embed.colour = discord.Colour.from_str(str(spec["colour"]))
		except ValueError:
			pass
	if spec.get("author"):
		embed.set_author(name=text("author", 256) or "", icon_url=text("author_icon", 2048))
	if spec.get("thumbnail"):
		embed.set_thumbnail(url=text("thumbnail", 2048))
	if spec.get("image"):
		embed.set_image(url=text("image", 2048))
	if spec.get("footer"):
		embed.set_footer(text=text("footer", 2048) or "", icon_url=text("footer_icon", 2048))
	for field in (spec.get("fields") or [])[:25]:
		name = _placeholders(str(field.get("name") or ""), message, trigger)[:256]
		body = _placeholders(str(field.get("value") or ""), message, trigger)[:1024]
		if name and body:
			embed.add_field(name=name, value=body, inline=bool(field.get("inline")))
	return embed

def pack_actions(actions: list, otherwise: list, elifs: list | None = None) -> list | dict:
	"""How a rule's actions are stored. Without an else it stays the plain list it
	has always been, so existing rules read back unchanged."""
	if not otherwise and not elifs:
		return actions
	packed = {"then": actions, "otherwise": otherwise}
	if elifs:
		packed["elifs"] = elifs
	return packed

def split_actions(stored) -> dict:
	"""The branches of a stored rule, whichever shape it was saved in."""
	if isinstance(stored, dict):
		return {
			"actions": stored.get("then") or [],
			"otherwise": stored.get("otherwise") or [],
			"elifs": stored.get("elifs") or [],
		}
	return {"actions": stored or [], "otherwise": [], "elifs": []}

def _first_branch(elifs: list, otherwise: list, message: discord.Message) -> list | None:
	"""The actions of the first else-if that matches, or the else. None means the
	rule has nothing to do for this message."""
	for branch in elifs:
		if evaluate(branch.get("conditions") or {}, message):
			return branch.get("actions") or None
	return otherwise or None

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

def forget_guild(guild_id: int):
	"""Drop a guild's parsed rules after an edit, so the next message reparses."""
	_parsed.pop(guild_id, None)

def _parse_rules(guild_id: int, rules: list) -> tuple[list, bool]:
	"""Rules as the dispatcher wants them, parsed once until the guild is forgotten."""
	cached = _parsed.get(guild_id)
	if cached is not None:
		return cached[0], cached[1]

	parsed = []
	wants_embeds = False
	for rule_id, name, _enabled, priority, conditions, actions, cooldown in rules:
		try:
			tree = json.loads(conditions)
			steps = json.loads(actions)
		except json.JSONDecodeError:
			continue
		branches = split_actions(steps)
		parsed.append((
			rule_id, name, tree, branches["actions"], branches["otherwise"],
			branches["elifs"], priority, cooldown, thread_mode(tree),
		))
		wants_embeds = wants_embeds or needs_embeds(tree) or any(
			needs_embeds(branch.get("conditions") or {}) for branch in branches["elifs"]
		)

	_parsed[guild_id] = (parsed, wants_embeds)
	return parsed, wants_embeds

async def run_action(action: dict, message: discord.Message, trigger: str | None = None) -> bool:
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
		allowed = _permitted_mentions(message, str(value or ""))
		allowed.replied_user = bool(action.get("mention", False))
		await message.reply(
			_placeholders(str(value or ""), message, trigger) or None,
			embed=_build_embed(action.get("embed"), message, trigger),
			allowed_mentions=allowed,
			suppress_embeds=bool(action.get("suppress_embeds", False)),
		)
		return True

	if kind == "send":
		await message.channel.send(
			_placeholders(str(value or ""), message, trigger) or None,
			embed=_build_embed(action.get("embed"), message, trigger),
			allowed_mentions=_permitted_mentions(message, str(value or "")),
		)
		return True

	if kind == "thread":
		name = _placeholders(str(value or "Discussion"), message, trigger)[:100]
		thread = await message.create_thread(name=name)
		opener = action.get("message")
		if opener:
			await thread.send(
				_placeholders(str(opener), message, trigger),
				allowed_mentions=_permitted_mentions(message, str(opener)),
			)
		return True

	if member is None:
		return False

	if kind == "add_role":
		roles = _assignable(message.guild, _ids(value))
		if roles:
			await member.add_roles(*roles, reason="Autoresponse")
		return False

	if kind == "remove_role":
		roles = _assignable(message.guild, _ids(value))
		if roles:
			await member.remove_roles(*roles, reason="Autoresponse")
		return False

	if kind == "kick":
		await member.kick(reason=_placeholders(str(value or "Autoresponse"), message, trigger))
		return False

	if kind == "ban":
		await member.ban(
			reason=_placeholders(str(value or "Autoresponse"), message, trigger),
			delete_message_seconds=int(action.get("delete_seconds", 0)),
		)
		return False

	if kind == "timeout":
		# Discord caps a timeout at 28 days
		seconds = min(int(value or 60), 28 * 24 * 60 * 60)
		await member.timeout(timedelta(seconds=seconds), reason="Autoresponse")
		return False

	return False

####### =================================================================== #######

async def wait_for_embeds(bot: discord.Client, message: discord.Message) -> discord.Message:
	"""Waits for Discord to attach a link's embed. Returns the message."""
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

	# only wait for embeds if a rule actually asks about them
	parsed, wants_embeds = _parse_rules(message.guild.id, rules)

	if wants_embeds:
		message = await wait_for_embeds(bot, message)

	spoken_at = None
	for rule_id, name, tree, steps, otherwise, elifs, priority, cooldown, threads in parsed:
		try:
			if not _thread_ok(threads, message):
				continue
			if not evaluate(tree, message):
				if not in_scope(tree, message):
					continue
				steps = _first_branch(elifs, otherwise, message)
				if steps is None:
					continue
			if on_cooldown(rule_id, message.author.id, cooldown):
				continue
			# rules of equal priority all answer, a higher one blocks the ones lower
			spoke = False
			outranked = spoken_at is not None and priority < spoken_at
			for action in steps[:MAX_ACTIONS]:
				if action.get("type") in SPEAKING_ACTIONS and outranked:
					continue
				try:
					if await run_action(action, message, find_trigger(tree)):
						spoke = True
				except discord.Forbidden:
					# missing permissions for this action only
					continue
				except discord.NotFound:
					# the message went away, so nothing left to act on
					return
			if spoke and spoken_at is None:
				spoken_at = priority
		except Exception:
			print(f"Autoresponse rule {rule_id} ({name}) failed:")
			traceback.print_exc()

####### =================================================================== #######

MAX_RULES = 50
MAX_NAME = 60
MAX_TEXT = 2000
MAX_CONDITIONS = 30
MAX_BRANCHES = 5

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
	"""Rebuilds a condition from scratch, so anything not in the allow-list is dropped."""
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
			raise ValueError("A rule needs at least one condition inside it.")
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

EMBED_TEXT = {
	"title": 256, "description": 4096, "url": 2048, "author": 256, "author_icon": 2048,
	"thumbnail": 2048, "image": 2048, "footer": 2048, "footer_icon": 2048,
}

def _clean_embed(spec) -> dict:
	if not isinstance(spec, dict):
		raise ValueError("That embed isn't valid.")  # noqa: TRY004

	cleaned = {}
	for key, limit in EMBED_TEXT.items():
		text = str(spec.get(key) or "").strip()
		if text:
			cleaned[key] = text[:limit]

	colour = str(spec.get("colour") or "").strip()
	if colour:
		if not re.fullmatch(r"#?[0-9a-fA-F]{6}", colour):
			raise ValueError("An embed colour looks like #5865F2.")
		cleaned["colour"] = colour if colour.startswith("#") else f"#{colour}"

	fields = []
	for field in (spec.get("fields") or [])[:25]:
		if not isinstance(field, dict):
			continue
		name = str(field.get("name") or "").strip()[:256]
		value = str(field.get("value") or "").strip()[:1024]
		if name and value:
			fields.append({"name": name, "value": value, "inline": bool(field.get("inline"))})
	if fields:
		cleaned["fields"] = fields

	if not cleaned:
		raise ValueError("An embed needs at least one thing in it.")
	return cleaned

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
		if not value and not (kind in ("reply", "send") and action.get("embed")):
			raise ValueError("That action needs some text.")
		cleaned = {"type": kind, "value": value[:MAX_TEXT]}
		if kind in ("reply", "send") and action.get("embed") is not None:
			cleaned["embed"] = _clean_embed(action["embed"])
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

def _no_repeats(node: dict, seen: set | None = None):
	"""One of each condition per group, matching what the editor offers."""
	if seen is None:
		seen = set()
	kind = node.get("type")
	children = node.get("children") or []
	# a lone not() is the editor's "doesn't" toggle. It shares the parent's group so
	# it can't be repeated either, but counts apart from the plain condition, since
	# "has role X" and "doesn't have role Y" together is a normal thing to want
	if kind == "not" and len(children) == 1:
		inner = children[0]
		label = f"not {inner.get('type')}"
		if label in seen:
			raise ValueError(f"A rule can only have one {str(inner.get('type')).replace('_', ' ')} condition.")
		seen.add(label)
		_no_repeats(inner, set())
		return
	if kind in ("all", "any", "not"):
		inner = set()
		for child in children:
			_no_repeats(child, inner)
		return
	if kind in seen:
		raise ValueError(f"A rule can only have one {kind.replace('_', ' ')} condition.")
	seen.add(kind)

def _clean_actions(raw, empty_error: str | None) -> list:
	"""Checks one list of actions, for either branch of a rule."""
	if not isinstance(raw, list) or not raw:
		if empty_error:
			raise ValueError(empty_error)
		return []
	if len(raw) > MAX_ACTIONS:
		raise ValueError(f"A rule can have at most {MAX_ACTIONS} actions.")
	actions = [_clean_action(action) for action in raw]

	kinds = [action["type"] for action in actions]
	duplicate = next((kind for kind in kinds if kinds.count(kind) > 1), None)
	if duplicate:
		raise ValueError(f"A rule can only have one {duplicate.replace('_', ' ')} action.")
	if "reply" in kinds and "send" in kinds:
		raise ValueError("A rule can reply or send a message, not both.")
	return actions

def validate(body: dict) -> dict:
	"""Checks a rule from the website, raising ValueError with something to show the user."""
	name = str(body.get("name") or "").strip()
	if not name:
		raise ValueError("Give the autoresponse a name.")

	conditions = _clean_condition(body.get("conditions"))
	_no_repeats(conditions)
	if conditions["type"] not in ("all", "any", "not"):
		# always hand the engine a group, so the shape is predictable
		conditions = {"type": "all", "children": [conditions]}
	threads = body.get("threads")
	conditions["threads"] = threads if threads in THREAD_MODES else "exclude"

	actions = _clean_actions(body.get("actions"), "An autoresponse needs at least one action.")
	# the else branch is optional, and empty means the rule simply does nothing
	otherwise = _clean_actions(body.get("otherwise"), None) if body.get("otherwise") else []

	elifs = []
	for branch in (body.get("elifs") or [])[:MAX_BRANCHES]:
		if not isinstance(branch, dict):
			continue
		tree = _clean_condition(branch.get("conditions"))
		_no_repeats(tree)
		if tree["type"] not in ("all", "any", "not"):
			tree = {"type": "all", "children": [tree]}
		elifs.append({
			"conditions": tree,
			"actions": _clean_actions(branch.get("actions"), "An else if needs at least one action."),
		})

	try:
		priority = int(body.get("priority", 0))
		cooldown = max(0, min(int(body.get("cooldown", 0)), 24 * 60 * 60))
	except (TypeError, ValueError) as error:
		raise ValueError("Priority and cooldown have to be numbers.") from error

	return {
		"name": name[:MAX_NAME],
		"conditions": conditions,
		"actions": actions,
		"otherwise": otherwise,
		"elifs": elifs,
		"priority": max(-100, min(priority, 100)),
		"cooldown": cooldown,
		"enabled": bool(body.get("enabled", True)),
	}
