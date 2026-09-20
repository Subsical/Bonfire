import hashlib
import os
from datetime import datetime, timezone

import sqlcipher3 as sqlite3

DB_PATH = os.environ.get('BONFIRE_DB_PATH', 'data/bonfire.db')

def open_connection(path: str = DB_PATH):
	connection = sqlite3.connect(path)
	cursor = connection.cursor()
	cursor.execute(f"PRAGMA key=\"{os.environ['BONFIRE_DB_KEY']}\"")
	cursor.execute("PRAGMA journal_mode=WAL")
	cursor.execute("PRAGMA busy_timeout=5000")
	cursor.execute("PRAGMA synchronous=NORMAL")
	return connection, cursor

conn, cur = open_connection()

####### =================================================================== #######

MIGRATIONS = [
	[
		"""CREATE TABLE IF NOT EXISTS Polls (
			poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
			message_id INTEGER UNIQUE,
			channel_id INTEGER NOT NULL,
			question TEXT NOT NULL,
			options TEXT NOT NULL,
			thread_id INTEGER,
			creator_hash TEXT NOT NULL,
			expires_at TEXT NOT NULL,
			closed INTEGER NOT NULL DEFAULT 0,
			reply_count INTEGER NOT NULL DEFAULT 0,
			last_reply TEXT,
			last_reply_at TEXT,
			supports_replies INTEGER NOT NULL DEFAULT 1,
			guild_id INTEGER
		)""",
		"""CREATE TABLE IF NOT EXISTS Votes (
			poll_id INTEGER NOT NULL,
			voter_hash TEXT NOT NULL,
			option_index INTEGER NOT NULL,
			PRIMARY KEY (poll_id, voter_hash)
		)""",
		"""CREATE TABLE IF NOT EXISTS Reminders (
			reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id INTEGER NOT NULL,
			channel_id INTEGER NOT NULL,
			guild_id INTEGER,
			message TEXT NOT NULL,
			remind_at TEXT NOT NULL,
			repeat TEXT NOT NULL DEFAULT 'none',
			pre_offsets TEXT NOT NULL DEFAULT '',
			sent_offsets TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL
		)""",
	], [
		"CREATE INDEX IF NOT EXISTS idx_polls_open_expiry ON Polls(expires_at) WHERE closed = 0",
		"CREATE INDEX IF NOT EXISTS idx_reminders_remind_at ON Reminders(remind_at)",
		"CREATE INDEX IF NOT EXISTS idx_reminders_pre ON Reminders(remind_at) WHERE pre_offsets != ''",
		"CREATE INDEX IF NOT EXISTS idx_reminders_user ON Reminders(user_id, remind_at)",
		"CREATE INDEX IF NOT EXISTS idx_polls_guild ON Polls(guild_id, poll_id DESC)",
	], [
		"ALTER TABLE Polls ADD COLUMN creator_key TEXT",
		"CREATE INDEX IF NOT EXISTS idx_polls_creator ON Polls(creator_key, poll_id DESC)",
	], [
		"""CREATE TABLE IF NOT EXISTS Guilds (
			guild_id INTEGER PRIMARY KEY,
			name TEXT NOT NULL,
			icon TEXT,
			joined_at TEXT NOT NULL
		)""",
	], [
		"""CREATE TABLE IF NOT EXISTS DisabledModules (
			guild_id INTEGER NOT NULL,
			module TEXT NOT NULL,
			PRIMARY KEY (guild_id, module)
		)""",
	], [
		"""CREATE TABLE IF NOT EXISTS AutoResponses (
			rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
			guild_id INTEGER NOT NULL,
			name TEXT NOT NULL,
			enabled INTEGER NOT NULL DEFAULT 1,
			priority INTEGER NOT NULL DEFAULT 0,
			conditions TEXT NOT NULL,
			actions TEXT NOT NULL,
			cooldown INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL
		)""",
		"CREATE INDEX IF NOT EXISTS idx_autoresponses_guild ON AutoResponses(guild_id, priority, rule_id)",
	], [
		"""CREATE TABLE IF NOT EXISTS RolePanels (
			panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
			guild_id INTEGER NOT NULL,
			channel_id INTEGER NOT NULL,
			message_id INTEGER,
			owned INTEGER NOT NULL DEFAULT 1,
			title TEXT NOT NULL,
			content TEXT NOT NULL DEFAULT '',
			embed TEXT,
			style TEXT NOT NULL DEFAULT 'reaction',
			mode TEXT NOT NULL DEFAULT 'multiple',
			limit_count INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL
		)""",
		"""CREATE TABLE IF NOT EXISTS RolePanelOptions (
			panel_id INTEGER NOT NULL,
			role_id INTEGER NOT NULL,
			emoji TEXT,
			label TEXT NOT NULL DEFAULT '',
			description TEXT NOT NULL DEFAULT '',
			position INTEGER NOT NULL DEFAULT 0,
			PRIMARY KEY (panel_id, role_id)
		)""",
		"CREATE INDEX IF NOT EXISTS idx_rolepanels_guild ON RolePanels(guild_id, panel_id DESC)",
		"CREATE INDEX IF NOT EXISTS idx_rolepanels_message ON RolePanels(message_id)",
		"CREATE INDEX IF NOT EXISTS idx_rolepaneloptions ON RolePanelOptions(panel_id, position)",
	], [
		"ALTER TABLE RolePanels ADD COLUMN per_row INTEGER NOT NULL DEFAULT 0",
	], [
		"""CREATE TABLE IF NOT EXISTS UserSettings (
			user_id INTEGER PRIMARY KEY,
			timezone TEXT NOT NULL DEFAULT '',
			tz_manual INTEGER NOT NULL DEFAULT 0
		)""",
	], [
		"""CREATE TABLE IF NOT EXISTS RobloxFriends (
			roblox_id INTEGER PRIMARY KEY,
			last_used TEXT NOT NULL
		)""",
		"CREATE INDEX IF NOT EXISTS idx_robloxfriends_used ON RobloxFriends(last_used)",
	], [
		"""CREATE TABLE IF NOT EXISTS RobloxAccounts (
			user_id INTEGER PRIMARY KEY,
			roblox_id INTEGER NOT NULL,
			username TEXT NOT NULL,
			linked_at TEXT NOT NULL
		)""",
		"CREATE UNIQUE INDEX IF NOT EXISTS idx_robloxaccounts_roblox ON RobloxAccounts(roblox_id)",
	],
]

def migrate():
	version, = cur.execute("PRAGMA user_version").fetchone()
	for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
		for statement in statements:
			cur.execute(statement)
		cur.execute(f"PRAGMA user_version = {number}")
		conn.commit()
		print(f"Applied database migration {number}.")

migrate()

####### =================================================================== #######

def hash_voter(user_id: int, poll_id: int) -> str:
	"""One-way hash of (user, poll) so a voter/creator can't be traced back for full anonymity"""
	salt = os.environ['BONFIRE_HASH_SALT']
	raw = f"{user_id}:{poll_id}:{salt}".encode()
	return hashlib.sha256(raw).hexdigest()

def creator_key(user_id: int) -> str:
	"""One-way hash of a user, stable across polls so the website can find the ones they made"""
	salt = os.environ['BONFIRE_CREATOR_SALT']
	raw = f"{user_id}:{salt}".encode()
	return hashlib.sha256(raw).hexdigest()

####### =================================================================== #######

def create_poll(channel_id: int, creator_id: int, question: str, options: list[str], expires_at: str, supports_replies: bool = True, guild_id: int | None = None) -> int:
	"""Inserts a new poll and returns its poll_id. message_id starts NULL, not 0, so a failed poll can't block a later one."""
	cur.execute("INSERT INTO Polls (message_id, channel_id, guild_id, question, options, creator_hash, creator_key, expires_at, supports_replies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (None, channel_id, guild_id, question, "\x1f".join(options), "", creator_key(creator_id), expires_at, int(supports_replies)))
	poll_id = cur.lastrowid
	cur.execute("UPDATE Polls SET creator_hash = ? WHERE poll_id = ?", (hash_voter(creator_id, poll_id), poll_id))
	conn.commit()
	return poll_id

def set_message_id(poll_id: int, message_id: int):
	cur.execute("UPDATE Polls SET message_id = ? WHERE poll_id = ?", (message_id, poll_id))
	conn.commit()

def expired_poll_ids() -> list[int]:
	now_iso = datetime.now(timezone.utc).isoformat()
	cur.execute("SELECT poll_id FROM Polls WHERE closed = 0 AND expires_at <= ?", (now_iso,))
	return [poll_id for poll_id, in cur.fetchall()]

def get_poll(poll_id: int, guild_id: int | None):
	"""Only returns a poll if it belongs to the given guild. guild_id=None skips that filter (cross-server admin override only)."""
	if guild_id is None:
		cur.execute("SELECT poll_id, message_id, channel_id, guild_id, question, options, thread_id, expires_at, closed FROM Polls WHERE poll_id = ?", (poll_id,))
	else:
		cur.execute("SELECT poll_id, message_id, channel_id, guild_id, question, options, thread_id, expires_at, closed FROM Polls WHERE poll_id = ? AND guild_id = ?", (poll_id, guild_id))
	return cur.fetchone()

def get_poll_render_data(poll_id: int):
	"""Gets (question, expires_at, supports_replies) which is what PollView needs to render"""
	cur.execute("SELECT question, expires_at, supports_replies FROM Polls WHERE poll_id = ?", (poll_id,))
	return cur.fetchone()

def get_poll_message_ref(poll_id: int):
	"""Gets (channel_id, message_id, closed, guild_id) for redrawing a poll's live Discord message"""
	cur.execute("SELECT channel_id, message_id, closed, guild_id FROM Polls WHERE poll_id = ?", (poll_id,))
	return cur.fetchone()

def get_poll_options(poll_id: int) -> list[str]:
	cur.execute("SELECT options FROM Polls WHERE poll_id = ?", (poll_id,))
	options_raw, = cur.fetchone()
	return options_raw.split("\x1f")

def get_poll_reply_thread(poll_id: int):
	"""Gets (channel_id, thread_id, question) for the reply modal, to find or create the replies thread."""
	cur.execute("SELECT channel_id, thread_id, question FROM Polls WHERE poll_id = ?", (poll_id,))
	return cur.fetchone()

def get_poll_reply_preview(poll_id: int):
	"""Gets (reply_count, last_reply, last_reply_at) for the reply-preview card."""
	cur.execute("SELECT reply_count, last_reply, last_reply_at FROM Polls WHERE poll_id = ?", (poll_id,))
	return cur.fetchone()

def get_poll_creator_hash(poll_id: int) -> str:
	cur.execute("SELECT creator_hash FROM Polls WHERE poll_id = ?", (poll_id,))
	creator_hash, = cur.fetchone()
	return creator_hash

def list_polls(guild_id: int | None) -> list[tuple]:
	"""Only lists polls belonging to the given guild. guild_id=None lists every guild's polls. Each row includes its own guild_id for building jump links."""
	if guild_id is None:
		cur.execute("SELECT poll_id, question, closed, expires_at, channel_id, message_id, guild_id FROM Polls ORDER BY poll_id DESC")
	else:
		cur.execute("SELECT poll_id, question, closed, expires_at, channel_id, message_id, guild_id FROM Polls WHERE guild_id = ? ORDER BY poll_id DESC", (guild_id,))
	return cur.fetchall()

def is_closed(poll_id: int) -> bool:
	cur.execute("SELECT closed FROM Polls WHERE poll_id = ?", (poll_id,))
	closed, = cur.fetchone()
	return bool(closed)

####### ========================= dashboard reads ========================= #######

# Paged, unlike list_polls
def polls_by_creator(key: str, limit: int = 50, offset: int = 0) -> list[tuple]:
	cur.execute(
		"SELECT poll_id, question, closed, expires_at, channel_id, message_id, guild_id "
		"FROM Polls WHERE creator_key = ? ORDER BY poll_id DESC LIMIT ? OFFSET ?",
		(key, limit, offset)
	)
	return cur.fetchall()

def polls_by_guild(guild_id: int, limit: int = 50, offset: int = 0) -> list[tuple]:
	cur.execute(
		"SELECT poll_id, question, closed, expires_at, channel_id, message_id, guild_id "
		"FROM Polls WHERE guild_id = ? ORDER BY poll_id DESC LIMIT ? OFFSET ?",
		(guild_id, limit, offset)
	)
	return cur.fetchall()

def count_polls_by_creator(key: str) -> int:
	cur.execute("SELECT COUNT(*) FROM Polls WHERE creator_key = ?", (key,))
	total, = cur.fetchone()
	return total

def count_polls_by_guild(guild_id: int) -> int:
	cur.execute("SELECT COUNT(*) FROM Polls WHERE guild_id = ?", (guild_id,))
	total, = cur.fetchone()
	return total

def owns_poll(poll_id: int, key: str) -> bool:
	cur.execute("SELECT 1 FROM Polls WHERE poll_id = ? AND creator_key = ?", (poll_id, key))
	return cur.fetchone() is not None

def guilds_with_polls() -> list[int]:
	cur.execute("SELECT DISTINCT guild_id FROM Polls WHERE guild_id IS NOT NULL")
	return [guild_id for guild_id, in cur.fetchall()]

def count_reminders_by_guild(guild_id: int) -> int:
	cur.execute("SELECT COUNT(*) FROM Reminders WHERE guild_id = ?", (guild_id,))
	total, = cur.fetchone()
	return total

def set_question(poll_id: int, question: str):
	cur.execute("UPDATE Polls SET question = ? WHERE poll_id = ?", (question, poll_id))
	conn.commit()

def set_options(poll_id: int, options: list[str]):
	cur.execute("UPDATE Polls SET options = ? WHERE poll_id = ?", ("\x1f".join(options), poll_id))
	conn.commit()

def set_expires_at(poll_id: int, expires_at: str):
	cur.execute("UPDATE Polls SET expires_at = ? WHERE poll_id = ?", (expires_at, poll_id))
	conn.commit()

def set_closed(poll_id: int, closed: bool):
	cur.execute("UPDATE Polls SET closed = ? WHERE poll_id = ?", (int(closed), poll_id))
	conn.commit()

def set_thread_id(poll_id: int, thread_id: int):
	cur.execute("UPDATE Polls SET thread_id = ? WHERE poll_id = ?", (thread_id, poll_id))
	conn.commit()

def add_reply(poll_id: int, reply_text: str, replied_at: str):
	cur.execute("UPDATE Polls SET reply_count = reply_count + 1, last_reply = ?, last_reply_at = ? WHERE poll_id = ?", (reply_text, replied_at, poll_id))
	conn.commit()

def delete_poll(poll_id: int):
	cur.execute("DELETE FROM Votes WHERE poll_id = ?", (poll_id,))
	cur.execute("DELETE FROM Polls WHERE poll_id = ?", (poll_id,))
	conn.commit()

####### =================================================================== #######

def option_counts(poll_id: int, num_options: int) -> list[int]:
	cur.execute("SELECT option_index, COUNT(*) FROM Votes WHERE poll_id = ? GROUP BY option_index", (poll_id,))
	tally = dict(cur.fetchall())
	return [tally.get(i, 0) for i in range(num_options)]

def total_votes(poll_id: int) -> int:
	cur.execute("SELECT COUNT(*) FROM Votes WHERE poll_id = ?", (poll_id,))
	total, = cur.fetchone()
	return total

def cast_vote(poll_id: int, voter_hash: str, option_index: int):
	cur.execute(
		"INSERT INTO Votes (poll_id, voter_hash, option_index) VALUES (?, ?, ?) "
		"ON CONFLICT(poll_id, voter_hash) DO UPDATE SET option_index = excluded.option_index",
		(poll_id, voter_hash, option_index)
	)
	conn.commit()

####### =================================================================== #######

def create_reminder(user_id: int, channel_id: int, guild_id: int | None, message: str, remind_at: str, repeat: str, pre_offsets: list[int]) -> int:
	"""Inserts a reminder and returns its reminder_id."""
	cur.execute(
		"INSERT INTO Reminders (user_id, channel_id, guild_id, message, remind_at, repeat, pre_offsets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
		(user_id, channel_id, guild_id, message, remind_at, repeat, ",".join(str(o) for o in pre_offsets), datetime.now(timezone.utc).isoformat())
	)
	conn.commit()
	return cur.lastrowid

def due_reminders() -> list[tuple]:
	"""Every reminder whose main time has arrived."""
	now_iso = datetime.now(timezone.utc).isoformat()
	cur.execute("SELECT reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_offsets, sent_offsets FROM Reminders WHERE remind_at <= ?", (now_iso,))
	return cur.fetchall()

def pending_reminders() -> list[tuple]:
	"""Reminders that haven't fired yet but have pre-reminders still to send."""
	now_iso = datetime.now(timezone.utc).isoformat()
	cur.execute("SELECT reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_offsets, sent_offsets FROM Reminders WHERE remind_at > ? AND pre_offsets != ''", (now_iso,))
	return cur.fetchall()

def get_reminder(reminder_id: int, user_id: int | None = None):
	"""Only returns a reminder if it belongs to the given user. user_id=None skips that filter."""
	if user_id is None:
		cur.execute("SELECT reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_offsets, sent_offsets FROM Reminders WHERE reminder_id = ?", (reminder_id,))
	else:
		cur.execute("SELECT reminder_id, user_id, channel_id, guild_id, message, remind_at, repeat, pre_offsets, sent_offsets FROM Reminders WHERE reminder_id = ? AND user_id = ?", (reminder_id, user_id))
	return cur.fetchone()

def list_reminders(user_id: int) -> list[tuple]:
	cur.execute("SELECT reminder_id, message, remind_at, repeat, pre_offsets, channel_id, guild_id FROM Reminders WHERE user_id = ? ORDER BY remind_at", (user_id,))
	return cur.fetchall()

def set_remind_at(reminder_id: int, remind_at: str):
	"""Moves a repeating reminder to its next occurrence, and clears the pre-reminders it already sent."""
	cur.execute("UPDATE Reminders SET remind_at = ?, sent_offsets = '' WHERE reminder_id = ?", (remind_at, reminder_id))
	conn.commit()

def set_sent_offsets(reminder_id: int, offsets: list[int]):
	cur.execute("UPDATE Reminders SET sent_offsets = ? WHERE reminder_id = ?", (",".join(str(o) for o in offsets), reminder_id))
	conn.commit()

def delete_reminder(reminder_id: int):
	cur.execute("DELETE FROM Reminders WHERE reminder_id = ?", (reminder_id,))
	conn.commit()

def update_reminder(reminder_id: int, user_id: int, **fields):
	"""Scoped to the owner, so a reminder id from elsewhere can't be edited through it."""
	allowed = {"message", "remind_at", "repeat", "pre_offsets", "channel_id", "guild_id"}
	changes = {key: value for key, value in fields.items() if key in allowed}
	if not changes:
		return False
	if "pre_offsets" in changes and isinstance(changes["pre_offsets"], list):
		changes["pre_offsets"] = ",".join(str(o) for o in changes["pre_offsets"])
	assignments = ", ".join(f"{key} = ?" for key in changes)
	# a changed time means the early nudges it already sent no longer apply
	cur.execute(
		f"UPDATE Reminders SET {assignments}, sent_offsets = '' WHERE reminder_id = ? AND user_id = ?",  # noqa: S608
		(*changes.values(), reminder_id, user_id),
	)
	conn.commit()
	return cur.rowcount > 0

def delete_reminder_for(reminder_id: int, user_id: int) -> bool:
	cur.execute("DELETE FROM Reminders WHERE reminder_id = ? AND user_id = ?", (reminder_id, user_id))
	conn.commit()
	return cur.rowcount > 0

####### =================================================================== #######

def sync_guilds(guilds: list[tuple]):
	"""Replaces the stored server list with what the bot can currently see."""
	cur.execute("DELETE FROM Guilds")
	cur.executemany(
		"INSERT INTO Guilds (guild_id, name, icon, joined_at) VALUES (?, ?, ?, ?)",
		[(gid, name, icon, datetime.now(timezone.utc).isoformat()) for gid, name, icon in guilds]
	)
	conn.commit()

def add_guild(guild_id: int, name: str, icon: str | None):
	cur.execute(
		"INSERT INTO Guilds (guild_id, name, icon, joined_at) VALUES (?, ?, ?, ?) "
		"ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name, icon = excluded.icon",
		(guild_id, name, icon, datetime.now(timezone.utc).isoformat())
	)
	conn.commit()

def remove_guild(guild_id: int):
	cur.execute("DELETE FROM Guilds WHERE guild_id = ?", (guild_id,))
	conn.commit()

def disabled_modules(guild_id: int) -> set[str]:
	cur.execute("SELECT module FROM DisabledModules WHERE guild_id = ?", (guild_id,))
	return {module for module, in cur.fetchall()}

def set_module(guild_id: int, module: str, enabled: bool):
	if enabled:
		cur.execute("DELETE FROM DisabledModules WHERE guild_id = ? AND module = ?", (guild_id, module))
	else:
		cur.execute("INSERT OR IGNORE INTO DisabledModules (guild_id, module) VALUES (?, ?)", (guild_id, module))
	conn.commit()

def module_enabled(guild_id: int | None, module: str) -> bool:
	"""Check if a module is enabled in the given guild."""
	if guild_id is None:
		return True
	keys = [module]
	if ":" in module:
		keys.append(module.split(":", 1)[0])
	placeholders = ",".join("?" * len(keys))
	cur.execute(f"SELECT 1 FROM DisabledModules WHERE guild_id = ? AND module IN ({placeholders})", (guild_id, *keys))  # noqa: S608
	return cur.fetchone() is None

####### ============================ autoresponses ========================= #######

def create_rule(guild_id: int, name: str, conditions: str, actions: str, priority: int = 0, cooldown: int = 0) -> int:
	cur.execute(
		"INSERT INTO AutoResponses (guild_id, name, conditions, actions, priority, cooldown, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
		(guild_id, name, conditions, actions, priority, cooldown, datetime.now(timezone.utc).isoformat()),
	)
	conn.commit()
	return cur.lastrowid

def guild_rules(guild_id: int, only_enabled: bool = True) -> list[tuple]:
	"""Ordered the way they're evaluated: priority first, then oldest."""
	query = "SELECT rule_id, name, enabled, priority, conditions, actions, cooldown FROM AutoResponses WHERE guild_id = ?"
	if only_enabled:
		query += " AND enabled = 1"
	cur.execute(query + " ORDER BY priority DESC, rule_id", (guild_id,))
	return cur.fetchall()

def get_rule(rule_id: int, guild_id: int):
	"""Scoped to the guild, so a rule id from elsewhere can't be read through it."""
	cur.execute(
		"SELECT rule_id, name, enabled, priority, conditions, actions, cooldown FROM AutoResponses WHERE rule_id = ? AND guild_id = ?",
		(rule_id, guild_id),
	)
	return cur.fetchone()

def update_rule(rule_id: int, guild_id: int, **fields):
	allowed = {"name", "enabled", "priority", "conditions", "actions", "cooldown"}
	changes = {key: value for key, value in fields.items() if key in allowed}
	if not changes:
		return
	assignments = ", ".join(f"{key} = ?" for key in changes)
	cur.execute(
		f"UPDATE AutoResponses SET {assignments} WHERE rule_id = ? AND guild_id = ?",  # noqa: S608
		(*changes.values(), rule_id, guild_id),
	)
	conn.commit()

def delete_rule(rule_id: int, guild_id: int) -> bool:
	cur.execute("DELETE FROM AutoResponses WHERE rule_id = ? AND guild_id = ?", (rule_id, guild_id))
	conn.commit()
	return cur.rowcount > 0

def count_rules(guild_id: int) -> int:
	cur.execute("SELECT COUNT(*) FROM AutoResponses WHERE guild_id = ?", (guild_id,))
	total, = cur.fetchone()
	return total

####### =========================== reaction roles ========================= #######

def create_panel(guild_id: int, channel_id: int, title: str, style: str, mode: str, limit_count: int = 0, content: str = "", embed: str | None = None, owned: bool = True, message_id: int | None = None, per_row: int = 0) -> int:
	cur.execute(
		"INSERT INTO RolePanels (guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
		(guild_id, channel_id, message_id, int(owned), title, content, embed, style, mode, limit_count, per_row, datetime.now(timezone.utc).isoformat()),
	)
	conn.commit()
	return cur.lastrowid

def set_panel_message(panel_id: int, message_id: int):
	cur.execute("UPDATE RolePanels SET message_id = ? WHERE panel_id = ?", (message_id, panel_id))
	conn.commit()

def get_panel(panel_id: int, guild_id: int | None = None):
	"""Scoped to the guild, so a panel id from elsewhere can't be read through it."""
	if guild_id is None:
		cur.execute("SELECT panel_id, guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row FROM RolePanels WHERE panel_id = ?", (panel_id,))
	else:
		cur.execute("SELECT panel_id, guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row FROM RolePanels WHERE panel_id = ? AND guild_id = ?", (panel_id, guild_id))
	return cur.fetchone()

def panel_by_message(message_id: int):
	cur.execute("SELECT panel_id, guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row FROM RolePanels WHERE message_id = ?", (message_id,))
	return cur.fetchone()

def guild_panels(guild_id: int) -> list[tuple]:
	cur.execute("SELECT panel_id, guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row FROM RolePanels WHERE guild_id = ? ORDER BY panel_id DESC", (guild_id,))
	return cur.fetchall()

def count_panels(guild_id: int) -> int:
	cur.execute("SELECT COUNT(*) FROM RolePanels WHERE guild_id = ?", (guild_id,))
	total, = cur.fetchone()
	return total

def update_panel(panel_id: int, guild_id: int, **fields):
	allowed = {"title", "content", "embed", "style", "mode", "limit_count", "channel_id", "message_id", "per_row"}
	changes = {key: value for key, value in fields.items() if key in allowed}
	if not changes:
		return
	assignments = ", ".join(f"{key} = ?" for key in changes)
	cur.execute(
		f"UPDATE RolePanels SET {assignments} WHERE panel_id = ? AND guild_id = ?",  # noqa: S608
		(*changes.values(), panel_id, guild_id),
	)
	conn.commit()

####### =========================== user settings ========================= #######

def get_timezone(user_id: int) -> str:
	"""Their IANA zone, or '' when they've never had one set."""
	cur.execute("SELECT timezone FROM UserSettings WHERE user_id = ?", (user_id,))
	row = cur.fetchone()
	return row[0] if row else ""

def timezone_is_manual(user_id: int) -> bool:
	cur.execute("SELECT tz_manual FROM UserSettings WHERE user_id = ?", (user_id,))
	row = cur.fetchone()
	return bool(row and row[0])

def set_timezone(user_id: int, name: str, manual: bool) -> bool:
	"""Stores a zone. An automatic one never replaces a hand-picked one."""
	if not manual and timezone_is_manual(user_id):
		return False
	cur.execute(
		"""INSERT INTO UserSettings (user_id, timezone, tz_manual) VALUES (?, ?, ?)
		ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone, tz_manual = excluded.tz_manual""",
		(user_id, name, int(manual)),
	)
	conn.commit()
	return True

####### =================================================================== #######

def unowned_panels() -> list[tuple]:
	"""Panels sitting on a message Bonfire didn't record as its own."""
	cur.execute("SELECT panel_id, guild_id, channel_id, message_id FROM RolePanels WHERE owned = 0 AND message_id IS NOT NULL")
	return cur.fetchall()

def set_panel_owned(panel_id: int, owned: bool):
	"""Kept out of update_panel, since who wrote the message isn't the website's to say."""
	cur.execute("UPDATE RolePanels SET owned = ? WHERE panel_id = ?", (1 if owned else 0, panel_id))
	conn.commit()

def delete_panel(panel_id: int, guild_id: int) -> bool:
	cur.execute("DELETE FROM RolePanelOptions WHERE panel_id = ?", (panel_id,))
	cur.execute("DELETE FROM RolePanels WHERE panel_id = ? AND guild_id = ?", (panel_id, guild_id))
	conn.commit()
	return cur.rowcount > 0

def panel_options(panel_id: int) -> list[tuple]:
	cur.execute("SELECT role_id, emoji, label, description, position FROM RolePanelOptions WHERE panel_id = ? ORDER BY position, role_id", (panel_id,))
	return cur.fetchall()

def set_panel_options(panel_id: int, options: list[tuple]):
	"""Replaces a panel's roles wholesale, which is how the website saves them."""
	cur.execute("DELETE FROM RolePanelOptions WHERE panel_id = ?", (panel_id,))
	cur.executemany(
		"INSERT INTO RolePanelOptions (panel_id, role_id, emoji, label, description, position) VALUES (?, ?, ?, ?, ?, ?)",
		[(panel_id, role_id, emoji, label, description, position) for position, (role_id, emoji, label, description) in enumerate(options)],
	)
	conn.commit()

def add_panel_option(panel_id: int, role_id: int, emoji: str | None, label: str = "", description: str = "") -> bool:
	"""Puts a role at the end of a panel. False if it's already on it."""
	cur.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM RolePanelOptions WHERE panel_id = ?", (panel_id,))
	position, = cur.fetchone()
	cur.execute(
		"INSERT OR IGNORE INTO RolePanelOptions (panel_id, role_id, emoji, label, description, position) VALUES (?, ?, ?, ?, ?, ?)",
		(panel_id, role_id, emoji, label, description, position),
	)
	conn.commit()
	return cur.rowcount > 0

def remove_panel_option(panel_id: int, role_id: int) -> bool:
	cur.execute("DELETE FROM RolePanelOptions WHERE panel_id = ? AND role_id = ?", (panel_id, role_id))
	conn.commit()
	return cur.rowcount > 0

####### =================================================================== #######

def touch_roblox_friend(roblox_id: int):
	"""Marks someone as still using /roblox, so they aren't unfriended for being idle."""
	cur.execute(
		"INSERT INTO RobloxFriends (roblox_id, last_used) VALUES (?, ?) "
		"ON CONFLICT(roblox_id) DO UPDATE SET last_used = excluded.last_used",
		(roblox_id, datetime.now(timezone.utc).isoformat()),
	)
	conn.commit()

def stale_roblox_friends(before: str) -> list[int]:
	cur.execute("SELECT roblox_id FROM RobloxFriends WHERE last_used < ?", (before,))
	return [row[0] for row in cur.fetchall()]

def known_roblox_friends() -> set[int]:
	cur.execute("SELECT roblox_id FROM RobloxFriends")
	return {row[0] for row in cur.fetchall()}

def link_roblox_account(user_id: int, roblox_id: int, username: str) -> bool:
	"""Ties a Discord user to the Roblox account they proved they own."""
	owner = roblox_account_owner(roblox_id)
	if owner is not None and owner != user_id:
		return False
	cur.execute(
		"INSERT INTO RobloxAccounts (user_id, roblox_id, username, linked_at) VALUES (?, ?, ?, ?) "
		"ON CONFLICT(user_id) DO UPDATE SET roblox_id = excluded.roblox_id, "
		"username = excluded.username, linked_at = excluded.linked_at",
		(user_id, roblox_id, username, datetime.now(timezone.utc).isoformat()),
	)
	conn.commit()
	return True

def linked_roblox_account(user_id: int) -> tuple[int, str] | None:
	cur.execute("SELECT roblox_id, username FROM RobloxAccounts WHERE user_id = ?", (user_id,))
	return cur.fetchone()

def unlink_roblox_account(user_id: int) -> bool:
	cur.execute("DELETE FROM RobloxAccounts WHERE user_id = ?", (user_id,))
	conn.commit()
	return cur.rowcount > 0

def roblox_account_owner(roblox_id: int) -> int | None:
	"""Which Discord user proved they own this Roblox account, if any."""
	cur.execute("SELECT user_id FROM RobloxAccounts WHERE roblox_id = ?", (roblox_id,))
	row = cur.fetchone()
	return row[0] if row else None

def forget_roblox_friend(roblox_id: int):
	cur.execute("DELETE FROM RobloxFriends WHERE roblox_id = ?", (roblox_id,))
	conn.commit()
