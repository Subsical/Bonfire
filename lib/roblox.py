import asyncio
import os
import re
import time

import aiohttp

HOSTS = ("roblox.com", "roproxy.com")
TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
MAX_USERNAME_LENGTH = 20
MAX_HISTORY_PAGES = 3
HISTORY_CACHE_SECONDS = 60 * 60
HISTORY_CACHE_SIZE = 500

OFFLINE, ONLINE, IN_GAME, IN_STUDIO = 0, 1, 2, 3
JOB_ID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
PLACE_IN_LINK = re.compile(r"(?:roblox\.com/games/|placeId=)(\d+)", re.I)
JOB_IN_LINK = re.compile(r"(?:gameInstanceId|jobId)=([0-9a-fA-F-]{36})", re.I)

class RobloxError(Exception):
	"""Something the user should be told"""

async def _get(session: aiohttp.ClientSession, subdomain: str, path: str, **params):
	"""Tries the real API, then the proxy as a fallback."""
	last = None
	for host in HOSTS:
		try:
			async with session.get(f"https://{subdomain}.{host}{path}", params=params) as resp:
				if resp.status == 200:
					return await resp.json(content_type=None)
				last = resp.status
		except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
			last = error
	raise RobloxError(f"Roblox isn't answering right now ({last}).")

async def _post(session: aiohttp.ClientSession, subdomain: str, path: str, body: dict):
	last = None
	for host in HOSTS:
		try:
			async with session.post(f"https://{subdomain}.{host}{path}", json=body) as resp:
				if resp.status == 200:
					return await resp.json(content_type=None)
				last = resp.status
		except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
			last = error
	raise RobloxError(f"Roblox isn't answering right now ({last}).")

####### =================================================================== #######

def parse_place(text: str) -> int:
	"""Get place id out of whatever someone pasted, link or bare number."""
	text = text.strip()
	if text.isdigit():
		return int(text)
	found = PLACE_IN_LINK.search(text)
	if found is None:
		raise RobloxError("That doesn't look like a Roblox game link or ID.")
	return int(found.group(1))

def parse_job(text: str | None) -> str | None:
	"""Get job id on its own, or pulled out of a share link."""
	if not text:
		return None
	text = text.strip()
	if JOB_ID.match(text):
		return text.lower()
	found = JOB_IN_LINK.search(text)
	if found is None or not JOB_ID.match(found.group(1)):
		raise RobloxError("That doesn't look like a server ID.")
	return found.group(1).lower()

def join_url(place_id: int, job_id: str | None = None) -> str:
	"""The link that opens the game. Roblox's own launcher handles it."""
	if job_id:
		return f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
	return f"https://www.roblox.com/games/start?placeId={place_id}"

def page_url(place_id: int) -> str:
	return f"https://www.roblox.com/games/{place_id}"

####### =================================================================== #######

async def place_details(session: aiohttp.ClientSession, place_id: int) -> dict:
	"""Name, creator and player count for a place."""
	universe = await _get(session, "apis", f"/universes/v1/places/{place_id}/universe")
	universe_id = universe.get("universeId")
	if not universe_id:
		raise RobloxError("I couldn't find a game with that ID.")

	games = await _get(session, "games", "/v1/games", universeIds=universe_id)
	rows = games.get("data") or []
	if not rows:
		raise RobloxError("I couldn't find a game with that ID.")
	game = rows[0]
	return {
		"universe_id": universe_id,
		"name": game.get("name") or "Unknown game",
		"creator": (game.get("creator") or {}).get("name") or "Unknown",
		"playing": game.get("playing") or 0,
		"max_players": game.get("maxPlayers") or 0,
		"root_place_id": game.get("rootPlaceId") or place_id,
	}

async def icon_url(session: aiohttp.ClientSession, universe_id: int) -> str | None:
	"""The game's thumbnail, or None when Roblox hasn't finished making one."""
	try:
		data = await _get(
			session, "thumbnails", "/v1/games/icons",
			universeIds=universe_id, size="512x512", format="Png", isCircular="false",
		)
	except RobloxError:
		return None
	rows = data.get("data") or []
	if rows and rows[0].get("state") == "Completed":
		return rows[0].get("imageUrl")
	return None

async def user_id(session: aiohttp.ClientSession, username: str) -> tuple[int, str]:
	"""Get the ID and display name for a username."""
	data = await _post(
		session, "users", "/v1/usernames/users",
		{"usernames": [username], "excludeBannedUsers": False},
	)
	rows = data.get("data") or []
	if not rows:
		raise RobloxError(f"There's no Roblox user called **{username}**.")
	return rows[0]["id"], rows[0].get("name") or username

async def presence(session: aiohttp.ClientSession, roblox_id: int) -> dict:
	"""Get the presence information for a Roblox user."""
	data = await _post(session, "presence", "/v1/presence/users", {"userIds": [roblox_id]})
	rows = data.get("userPresences") or []
	if not rows:
		raise RobloxError("Roblox wouldn't tell me where they are.")
	return rows[0]

async def profile(session: aiohttp.ClientSession, roblox_id: int) -> dict:
	"""Everything public about an account, for /lookup roblox."""
	async def optional(request):
		try:
			return await request
		except RobloxError:
			return None

	user, friends, followers, following, avatar, headshot, history, where = await asyncio.gather(
		_get(session, "users", f"/v1/users/{roblox_id}"),
		optional(_get(session, "friends", f"/v1/users/{roblox_id}/friends/count")),
		optional(_get(session, "friends", f"/v1/users/{roblox_id}/followers/count")),
		optional(_get(session, "friends", f"/v1/users/{roblox_id}/followings/count")),
		optional(_get(
			session, "thumbnails", "/v1/users/avatar",
			userIds=roblox_id, size="420x420", format="Png", isCircular="false",
		)),
		optional(_get(
			session, "thumbnails", "/v1/users/avatar-headshot",
			userIds=roblox_id, size="420x420", format="Png", isCircular="false",
		)),
		past_usernames(session, roblox_id),
		optional(presence(session, roblox_id)),
	)
	def image(response: dict | None) -> str | None:
		rows = (response or {}).get("data") or []
		return rows[0].get("imageUrl") if rows and rows[0].get("state") == "Completed" else None

	return {
		**user,
		"friends": (friends or {}).get("count"),
		"followers": (followers or {}).get("count"),
		"following": (following or {}).get("count"),
		"avatar": image(avatar),
		"headshot": image(headshot),
		"past_names": history,
		"presence": (where or {}).get("userPresenceType"),
		"location": (where or {}).get("lastLocation"),
	}

# roblox id -> (when it goes stale, names), since roblox only answers one history request a minute
_history_cache: dict[int, tuple[float, list[str]]] = {}

async def past_usernames(session: aiohttp.ClientSession, roblox_id: int) -> list[str]:
	"""Every name the account has had, newest first."""
	cached = _history_cache.get(roblox_id)
	if cached and cached[0] > time.monotonic():
		return cached[1]

	names, cursor, complete = [], None, False
	for _ in range(MAX_HISTORY_PAGES):
		params = {"limit": 100, "sortOrder": "Desc"}
		if cursor:
			params["cursor"] = cursor
		try:
			data = await _get(session, "users", f"/v1/users/{roblox_id}/username-history", **params)
		except RobloxError:
			break
		names += [row["name"] for row in data.get("data") or [] if row.get("name")]
		cursor = data.get("nextPageCursor")
		if not cursor:
			complete = True
			break

	# a list cut short by the limit shouldn't stop the next lookup from trying again
	if complete:
		_history_cache.pop(roblox_id, None)
		_history_cache[roblox_id] = (time.monotonic() + HISTORY_CACHE_SECONDS, names)
		if len(_history_cache) > HISTORY_CACHE_SIZE:
			del _history_cache[next(iter(_history_cache))]
	return names

def profile_url(roblox_id: int) -> str:
	return f"https://www.roblox.com/users/{roblox_id}/profile"

async def servers(session: aiohttp.ClientSession, place_id: int, limit: int = 10) -> list[dict]:
	"""Get public servers for a place, sorted by player count."""
	try:
		data = await _get(session, "games", f"/v1/games/{place_id}/servers/Public", limit=limit)
	except RobloxError:
		return []
	rows = data.get("data") or []
	open_servers = [row for row in rows if (row.get("playing") or 0) < (row.get("maxPlayers") or 0)]
	return sorted(open_servers, key=lambda row: row.get("playing") or 0)

####### =================================================================== #######

COOKIE = os.environ.get("ROBLOSECURITY", "")

_csrf = ""
_helper_id = 0

def helper_configured() -> bool:
	return bool(COOKIE)

def helper_url() -> str | None:
	"""The profile people friend to opt in, once we know who we are."""
	if not _helper_id:
		return None
	return f"https://www.roblox.com/users/{_helper_id}/profile"

async def _authed(session: aiohttp.ClientSession, method: str, url: str, body: dict | None = None):
	global _csrf
	if not COOKIE:
		raise RobloxError("Roblox cookie not found")
	for attempt in range(2):
		headers = {"Cookie": f".ROBLOSECURITY={COOKIE}", "X-CSRF-TOKEN": _csrf}
		try:
			async with session.request(method, url, headers=headers, json=body) as resp:
				if resp.status == 403 and attempt == 0:
					_csrf = resp.headers.get("X-CSRF-TOKEN", "")
					continue
				if resp.status in (401, 403):
					raise RobloxError("My Roblox account needs signing in again.")
				if resp.status != 200:
					raise RobloxError(f"Roblox turned that down ({resp.status}).")
				return await resp.json(content_type=None)
		except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
			raise RobloxError("Roblox isn't answering right now.") from error
	raise RobloxError("Roblox isn't answering right now.")

async def helper_id(session: aiohttp.ClientSession) -> int:
	"""Get who the helper account is, looked up once and kept"""
	global _helper_id
	if not _helper_id:
		me = await _authed(session, "GET", "https://users.roblox.com/v1/users/authenticated")
		_helper_id = me.get("id") or 0
	return _helper_id

async def accept_friend_requests(session: aiohttp.ClientSession) -> list[int]:
	"""Accepts everyone waiting, so they only have to ask once"""
	pending = await _authed(session, "GET", "https://friends.roblox.com/v1/my/friends/requests?limit=50")
	accepted = []
	for row in pending.get("data") or []:
		sender = row.get("id")
		if not sender:
			continue
		try:
			await _authed(session, "POST", f"https://friends.roblox.com/v1/users/{sender}/accept-friend-request")
			accepted.append(sender)
		except RobloxError:
			continue
	return accepted

async def friend_ids(session: aiohttp.ClientSession) -> list[int]:
	"""Get every user the helper account is actually friends with."""
	me = await helper_id(session)
	data = await _authed(session, "GET", f"https://friends.roblox.com/v1/users/{me}/friends")
	return [row["id"] for row in data.get("data") or [] if row.get("id")]

async def befriended(session: aiohttp.ClientSession, roblox_id: int) -> bool:
	"""Check whether the helper account is already this person's friend."""
	data = await _authed(session, "GET", f"https://friends.roblox.com/v1/users/{roblox_id}/friends")
	me = await helper_id(session)
	return any(friend.get("id") == me for friend in data.get("data") or [])

async def presence_as_helper(session: aiohttp.ClientSession, roblox_id: int) -> dict | None:
	"""Get presence information while signed in, which friends-only joins will answer."""
	if not COOKIE:
		return None
	try:
		data = await _authed(
			session, "POST", "https://presence.roblox.com/v1/presence/users",
			{"userIds": [roblox_id]},
		)
	except RobloxError:
		return None
	rows = data.get("userPresences") or []
	return rows[0] if rows else None

async def unfriend(session: aiohttp.ClientSession, roblox_id: int):
	await _authed(session, "POST", f"https://friends.roblox.com/v1/users/{roblox_id}/unfriend")

async def friend_count(session: aiohttp.ClientSession) -> int:
	"""Check how full the helper account's friend list is, against Roblox's 1000 cap."""
	me = await helper_id(session)
	data = await _authed(session, "GET", f"https://friends.roblox.com/v1/users/{me}/friends/count")
	return data.get("count") or 0

async def display_name(session: aiohttp.ClientSession, roblox_id: int) -> str:
	data = await _get(session, "users", f"/v1/users/{roblox_id}")
	return data.get("name") or str(roblox_id)
