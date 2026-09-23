import calendar
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from lib import database as db
from lib import theme

# what the timezone picker offers before anyone types, since the full list is 600 long
COMMON_ZONES = {
	"UTC", "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Madrid",
	"Europe/Paris", "Europe/Berlin", "Europe/Amsterdam", "Europe/Brussels",
	"Europe/Rome", "Europe/Stockholm", "Europe/Warsaw", "Europe/Athens",
	"Europe/Helsinki", "Europe/Kyiv", "Europe/Moscow", "Europe/Istanbul",
	"America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
	"America/Toronto", "America/Vancouver", "America/Sao_Paulo", "Asia/Tokyo",
	"Asia/Seoul", "Asia/Shanghai", "Asia/Singapore", "Asia/Kolkata", "Asia/Dubai",
	"Australia/Sydney", "Australia/Perth", "Pacific/Auckland",
}

MAX_MESSAGE_LENGTH = 500
MAX_PER_USER = 25
MAX_PRE_REMINDERS = 5

REPEATS = ["none", "daily", "weekly", "monthly", "yearly"]
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

####### =================================================================== #######

def parse_duration(text: str) -> int:
	"""Seconds in a duration like '10m', '1h30m' or '2d12h'. Raises ValueError if it isn't one."""
	cleaned = text.strip().lower().replace(" ", "")
	parts = re.findall(r"(\d+)([smhdw])", cleaned)
	if not parts or "".join(f"{n}{u}" for n, u in parts) != cleaned:
		raise ValueError(f"`{text.strip()}` is an invalid duration. Try something like `10m`, `1h30m` or `2d`.")

	total = sum(int(amount) * UNITS[unit] for amount, unit in parts)
	if total <= 0:
		raise ValueError("That duration has to be longer than zero.")
	return total

def is_absolute(text: str) -> bool:
	"""Whether this is a date someone typed, rather than a duration or a timestamp."""
	value = text.strip()
	if re.fullmatch(r"<t:\d+(?::[tTdDfFR])?>", value) or value.isdigit():
		return False
	return any(_parses(value, pattern) for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"))

def _parses(value: str, pattern: str) -> bool:
	try:
		datetime.strptime(value, pattern)
	except ValueError:
		return False
	return True

def valid_zone(name: str) -> bool:
	"""Whether this is a timezone the system knows, like Europe/Berlin."""
	try:
		ZoneInfo(name)
	except (ZoneInfoNotFoundError, ValueError):
		return False
	return True

def zone_of(user_id: int | None) -> ZoneInfo:
	"""Someone's timezone, falling back to UTC when they've never set one."""
	if user_id is None:
		return ZoneInfo("UTC")
	try:
		return ZoneInfo(db.get_timezone(user_id) or "UTC")
	except (ZoneInfoNotFoundError, ValueError):
		return ZoneInfo("UTC")

def parse_when(text: str, user_id: int | None = None) -> datetime:
	"""When a reminder should fire. Takes a duration, a unix timestamp, a Discord <t:...> stamp, or a plain date."""
	value = text.strip()

	stamp = re.fullmatch(r"<t:(\d+)(?::[tTdDfFR])?>", value)
	if stamp:
		value = stamp.group(1)

	if value.isdigit():
		try:
			return datetime.fromtimestamp(int(value), timezone.utc)
		except (ValueError, OSError, OverflowError):
			raise ValueError(f"`{text.strip()}` is not a valid timestamp.") from None

	# a date someone types is the time on their own clock, not UTC
	for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
		try:
			naive = datetime.strptime(value, pattern)
		except ValueError:
			continue
		return naive.replace(tzinfo=zone_of(user_id)).astimezone(timezone.utc)

	return datetime.now(timezone.utc) + timedelta(seconds=parse_duration(value))

def parse_pre_offsets(text: str | None) -> list[int]:
	"""The '1d, 1h' style list of how long before the reminder to also nudge."""
	if not text or text.strip().lower() == "none":
		return []
	offsets = {parse_duration(part) for part in text.split(",") if part.strip()}
	if len(offsets) > MAX_PRE_REMINDERS:
		raise ValueError(f"That's too many pre-reminders, you can only have {MAX_PRE_REMINDERS} at most.")
	return sorted(offsets, reverse=True)

def format_duration(seconds: int) -> str:
	for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
		if seconds % size == 0:
			return f"{seconds // size}{unit}"
	return f"{seconds}s"

####### =================================================================== #######

def add_months(when: datetime, months: int) -> datetime:
	"""Same day next month, clamped so the 31st doesn't fall off a short month."""
	month = when.month - 1 + months
	year = when.year + month // 12
	month = month % 12 + 1
	return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))

def next_occurrence(when: datetime, repeat: str) -> datetime | None:
	"""When a repeating reminder should next fire, or None if it doesn't repeat."""
	if repeat == "daily":
		return when + timedelta(days=1)
	if repeat == "weekly":
		return when + timedelta(weeks=1)
	if repeat == "monthly":
		return add_months(when, 1)
	if repeat == "yearly":
		return add_months(when, 12)
	return None

def catch_up(when: datetime, repeat: str) -> datetime | None:
	"""Skips missed occurrences, so a repeating reminder doesn't fire multiple times at once."""
	now = datetime.now(timezone.utc)
	while when <= now:
		following = next_occurrence(when, repeat)
		if following is None or following <= when:
			return None
		when = following
	return when

####### =================================================================== #######

# {{count:3}}, {{years:2018-08-29}}, {{user.ping}}
VARIABLE_PATTERN = re.compile(r"\{\{ *([a-z]+)(?:[.:]([^{}]*?))? *\}\}")
ELAPSED_UNITS = ("years", "months", "days")

def _elapsed(kind: str, start: str, now: datetime) -> str | None:
	"""How long since a date written into the message, or None if it isn't one."""
	try:
		since = datetime.fromisoformat(start.strip()).replace(tzinfo=timezone.utc)
	except ValueError:
		return None

	months = (now.year - since.year) * 12 + now.month - since.month
	# a month isn't up until the day comes round
	if now.day < since.day:
		months -= 1
	if kind == "years":
		return str(max(0, months // 12))
	if kind == "months":
		return str(max(0, months))
	return str(max(0, (now - since).days))

def _count_at(raw: str | None) -> int | None:
	"""The number a counter stands at. A bare {{count}} is one, a bad one is None."""
	value = (raw or "").strip()
	if not value:
		return 1
	try:
		return int(value)
	except ValueError:
		return None

def _counters(text: str) -> bool:
	"""Whether a message has a counter to rewrite, so only those get written back."""
	return any(
		match.group(1) == "count" and _count_at(match.group(2)) is not None
		for match in VARIABLE_PATTERN.finditer(text or "")
	)

def advance(text: str) -> str:
	"""The message with every counter moved on one, for the next time it repeats."""
	def bump(match: re.Match) -> str:
		current = _count_at(match.group(2))
		if match.group(1) != "count" or current is None:
			return match.group(0)
		return f"{{{{count:{current + 1}}}}}"

	moved = VARIABLE_PATTERN.sub(bump, text)
	# rolling into another digit can push a message that was already at the limit over it
	return text if len(moved) > MAX_MESSAGE_LENGTH else moved

def fill(text: str, user_id: int | None = None) -> str:
	"""A reminder's message with its variables filled in, as it gets sent."""
	local = datetime.now(zone_of(user_id))
	values = {
		("date", ""): local.strftime("%Y-%m-%d"),
		("time", ""): local.strftime("%H:%M"),
		("year", ""): str(local.year),
		("user", "ping"): f"<@{user_id}>" if user_id else "",
		("user", "mention"): f"<@{user_id}>" if user_id else "",
		("user", "id"): str(user_id) if user_id else "",
	}

	def swap(match: re.Match) -> str:
		kind, name = match.group(1), (match.group(2) or "").strip()
		if kind == "count":
			current = _count_at(name)
			return str(current) if current is not None else match.group(0)
		if kind in ELAPSED_UNITS:
			elapsed = _elapsed(kind, name, local)
			return elapsed if elapsed is not None else match.group(0)
		return values.get((kind, name), match.group(0))

	return VARIABLE_PATTERN.sub(swap, text)

####### =================================================================== #######

def parse_offsets(raw: str) -> list[int]:
	return [int(o) for o in raw.split(",") if o]

async def deliver(bot: discord.Client, reminder: tuple, pre_offset: int | None = None) -> bool:
	"""Posts a reminder in its channel, falling back to a DM if that's gone. False if neither worked."""
	# reminder = (reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_raw, sent_raw)
	_, user_id, channel_id, guild_id, message, remind_at, repeat, _, _ = reminder

	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	heading = "### ⏰ Coming up" if pre_offset is not None else "### 🔔 Reminder"
	container.add_item(discord.ui.TextDisplay(f"{heading}\n<@{user_id}>\n{fill(message, user_id)}"))

	footer = [f"<t:{int(datetime.fromisoformat(remind_at).timestamp())}:R>"]
	if pre_offset is not None:
		footer.append(f"{format_duration(pre_offset)} early")
	if repeat != "none":
		footer.append(f"repeats {repeat}")
	container.add_item(discord.ui.TextDisplay(f"-# {' • '.join(footer)}"))

	view = discord.ui.LayoutView(timeout=None)
	view.add_item(container)

	channel = bot.get_channel(channel_id)
	if channel is None and not (guild_id is not None and bot.get_guild(guild_id) is None):
		try:
			channel = await bot.fetch_channel(channel_id)
		except (discord.NotFound, discord.Forbidden, discord.HTTPException):
			channel = None

	if channel is not None:
		try:
			await channel.send(view=view, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=user_id)]))
			return True
		except (discord.Forbidden, discord.HTTPException):
			pass

	# fall back to reminder creator's DMs
	user = bot.get_user(user_id)
	if user is None:
		try:
			user = await bot.fetch_user(user_id)
		except discord.HTTPException:
			return False
	try:
		await user.send(view=view)
		return True
	except (discord.Forbidden, discord.HTTPException):
		return False

async def check_due(bot: discord.Client):
	"""Fires every reminder that's due, and any pre-reminders that have come around."""
	for reminder in db.pending_reminders():
		reminder_id, _, _, _, _, remind_at, _, pre_raw, sent_raw = reminder
		due = datetime.fromisoformat(remind_at)
		sent = parse_offsets(sent_raw)
		now = datetime.now(timezone.utc)

		for offset in parse_offsets(pre_raw):
			if offset in sent or due - timedelta(seconds=offset) > now:
				continue
			await deliver(bot, reminder, pre_offset=offset)
			sent.append(offset)
			db.set_sent_offsets(reminder_id, sent)

	for reminder in db.due_reminders():
		reminder_id, _, _, _, message, remind_at, repeat, _, _ = reminder
		sent = await deliver(bot, reminder)

		following = catch_up(datetime.fromisoformat(remind_at), repeat) if repeat != "none" else None
		if following is None:
			db.delete_reminder(reminder_id)
		else:
			db.set_remind_at(reminder_id, following.isoformat())
			# a number that was never delivered shouldn't be used up
			if sent and _counters(message):
				db.set_reminder_message(reminder_id, advance(message))
