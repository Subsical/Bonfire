import hashlib
import os
from datetime import datetime, timezone

import sqlcipher3 as sqlite3

conn = sqlite3.connect('bonfire.db')
cur = conn.cursor()
cur.execute(f"PRAGMA key=\"{os.environ['BONFIRE_DB_KEY']}\"")

cur.execute("""
	CREATE TABLE IF NOT EXISTS Polls (
		poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
		message_id INTEGER UNIQUE,
		channel_id INTEGER NOT NULL,
		guild_id INTEGER,
		question TEXT NOT NULL,
		options TEXT NOT NULL,
		thread_id INTEGER,
		creator_hash TEXT NOT NULL,
		expires_at TEXT NOT NULL,
		closed INTEGER NOT NULL DEFAULT 0,
		reply_count INTEGER NOT NULL DEFAULT 0,
		last_reply TEXT,
		last_reply_at TEXT,
		supports_replies INTEGER NOT NULL DEFAULT 1
	)
""")
cur.execute("""
	CREATE TABLE IF NOT EXISTS Votes (
		poll_id INTEGER NOT NULL,
		voter_hash TEXT NOT NULL,
		option_index INTEGER NOT NULL,
		PRIMARY KEY (poll_id, voter_hash)
	)
""")
conn.commit()

cur.execute("PRAGMA table_info(Polls)")
existing_columns = {row[1] for row in cur.fetchall()}
for column, definition in [
	("reply_count", "INTEGER NOT NULL DEFAULT 0"),
	("last_reply", "TEXT"),
	("last_reply_at", "TEXT"),
	("supports_replies", "INTEGER NOT NULL DEFAULT 1"),
	("guild_id", "INTEGER"),
]:
	if column not in existing_columns:
		cur.execute(f"ALTER TABLE Polls ADD COLUMN {column} {definition}")
conn.commit()

########## ======================================================================== ##########

def hash_voter(user_id: int, poll_id: int) -> str:
	"""One-way hash of (user, poll) so a voter/creator can't be traced back, but a repeat vote or the poll's own creator can still be recognized."""
	salt = os.environ['BONFIRE_HASH_SALT']
	raw = f"{user_id}:{poll_id}:{salt}".encode()
	return hashlib.sha256(raw).hexdigest()

########## ======================================================================== ##########

def create_poll(channel_id: int, creator_id: int, question: str, options: list[str], expires_at: str, supports_replies: bool = True, guild_id: int | None = None) -> int:
	"""Inserts a new poll and returns its poll_id."""
	cur.execute(
		"INSERT INTO Polls (message_id, channel_id, guild_id, question, options, creator_hash, expires_at, supports_replies) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
		(0, channel_id, guild_id, question, "\x1f".join(options), "", expires_at, int(supports_replies))
	)
	poll_id = cur.lastrowid
	cur.execute("UPDATE Polls SET creator_hash = ? WHERE poll_id = ?", (hash_voter(creator_id, poll_id), poll_id))
	conn.commit()
	return poll_id

def set_message_id(poll_id: int, message_id: int):
	cur.execute("UPDATE Polls SET message_id = ? WHERE poll_id = ?", (message_id, poll_id))
	conn.commit()

def all_poll_views_data() -> list[tuple]:
	"""Gets (poll_id, options, closed) for every stored poll, used to reattach persistent views on startup"""
	cur.execute("SELECT poll_id, options, closed FROM Polls")
	return cur.fetchall()

def expired_poll_ids() -> list[int]:
	now_iso = datetime.now(timezone.utc).isoformat()
	cur.execute("SELECT poll_id FROM Polls WHERE closed = 0 AND expires_at <= ?", (now_iso,))
	return [poll_id for poll_id, in cur.fetchall()]

def get_poll(poll_id: int, guild_id: int):
	"""Only returns a poll if it belongs to the given guild"""
	cur.execute("SELECT poll_id, message_id, channel_id, question, options, thread_id, expires_at, closed FROM Polls WHERE poll_id = ? AND guild_id = ?", (poll_id, guild_id))
	return cur.fetchone()

def get_poll_render_data(poll_id: int):
	"""Gets (question, expires_at, supports_replies) which is what PollView needs to render"""
	cur.execute("SELECT question, expires_at, supports_replies FROM Polls WHERE poll_id = ?", (poll_id,))
	return cur.fetchone()

def get_poll_message_ref(poll_id: int):
	"""Gets (channel_id, message_id, closed) for redrawing a poll's live Discord message"""
	cur.execute("SELECT channel_id, message_id, closed FROM Polls WHERE poll_id = ?", (poll_id,))
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

def list_polls(guild_id: int) -> list[tuple]:
	"""Only lists polls belonging to the given guild."""
	cur.execute("SELECT poll_id, question, closed, expires_at, channel_id, message_id FROM Polls WHERE guild_id = ? ORDER BY poll_id DESC", (guild_id,))
	return cur.fetchall()

def is_closed(poll_id: int) -> bool:
	cur.execute("SELECT closed FROM Polls WHERE poll_id = ?", (poll_id,))
	closed, = cur.fetchone()
	return bool(closed)

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
	cur.execute(
		"UPDATE Polls SET reply_count = reply_count + 1, last_reply = ?, last_reply_at = ? WHERE poll_id = ?",
		(reply_text, replied_at, poll_id)
	)
	conn.commit()

def delete_poll(poll_id: int):
	cur.execute("DELETE FROM Votes WHERE poll_id = ?", (poll_id,))
	cur.execute("DELETE FROM Polls WHERE poll_id = ?", (poll_id,))
	conn.commit()

########## ======================================================================== ##########

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
