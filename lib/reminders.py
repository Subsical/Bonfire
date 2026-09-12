import calendar
import re
from datetime import datetime, timedelta, timezone

import discord

from lib import database as db
from lib import theme

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

def parse_when(text: str) -> datetime:
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

	for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
		except ValueError:
			continue

	return datetime.now(timezone.utc) + timedelta(seconds=parse_duration(value))

def parse_pre_offsets(text: str | None) -> list[int]:
	"""The '1d, 1h' style list of how long before the reminder to also nudge."""
	if not text or text.strip().lower() == "none":
		return []
	offsets = {parse_duration(part) for part in text.split(",") if part.strip()}
	if len(offsets) > MAX_PRE_REMINDERS:
		raise ValueError(f"That's too many pre-reminders, {MAX_PRE_REMINDERS} is the most I'll take.")
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

def parse_offsets(raw: str) -> list[int]:
	return [int(o) for o in raw.split(",") if o]

async def deliver(bot: discord.Client, reminder: tuple, pre_offset: int | None = None) -> bool:
	"""Posts a reminder in its channel, falling back to a DM if that's gone. False if neither worked."""
	# reminder = (reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_raw, sent_raw)
	_, user_id, channel_id, guild_id, message, remind_at, repeat, _, _ = reminder

	container = discord.ui.Container(accent_color=theme.COLOR_MAIN)
	heading = "### ⏰ Coming up" if pre_offset is not None else "### 🔔 Reminder"
	container.add_item(discord.ui.TextDisplay(f"{heading}\n{message}"))

	footer = [f"<t:{int(datetime.fromisoformat(remind_at).timestamp())}:R>"]
	if pre_offset is not None:
		footer.append(f"{format_duration(pre_offset)} early")
	if repeat != "none":
		footer.append(f"repeats {repeat}")
	container.add_item(discord.ui.TextDisplay(f"-# {' • '.join(footer)}"))

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
			await channel.send(content=f"<@{user_id}>", view=view, allowed_mentions=discord.AllowedMentions(users=True))
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
		reminder_id, _, _, _, _, remind_at, repeat, _, _ = reminder
		await deliver(bot, reminder)

		following = catch_up(datetime.fromisoformat(remind_at), repeat) if repeat != "none" else None
		if following is None:
			db.delete_reminder(reminder_id)
		else:
			db.set_remind_at(reminder_id, following.isoformat())
