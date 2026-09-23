import asyncio
import html
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import quote, quote_plus

import aiohttp
import discord

from lib import roblox, theme

TIMEOUT = aiohttp.ClientTimeout(total=12, connect=5)
HEADERS = {"User-Agent": "Bonfire/1.0 (Discord bot; subsical@gmail.com)"}
MAX_QUERY_LENGTH = 100
DESCRIPTION_LENGTH = 500

DEEZER = "https://api.deezer.com"
SPOTIFY = "https://api.spotify.com/v1"
SPOTIFY_AUTH = "https://accounts.spotify.com/api/token"
SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
ANILIST = "https://graphql.anilist.co"
KITSU = "https://kitsu.app/api/edge"
WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"
ANILIST_CACHE_SECONDS = 60 * 60
ANILIST_CACHE_SIZE = 500
STEAM_APP_IN_LINK = re.compile(r"/app/(\d+)")
# anilist only matches whole words, and no title has "s4" in it
SEASON_SHORTHAND = re.compile(r"\bs(?:eason)?\s*0*(\d+)\b", re.I)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PRESENCE = {
	roblox.OFFLINE: theme.ROBLOX_OFFLINE,
	roblox.ONLINE: theme.ROBLOX_ONLINE,
	roblox.IN_GAME: theme.ROBLOX_INGAME,
	roblox.IN_STUDIO: theme.ROBLOX_STUDIO,
}
FORMATS = {"TV": "TV", "TV_SHORT": "TV short", "OVA": "OVA", "ONA": "ONA", "NOVEL": "Light novel", "ONE_SHOT": "One shot"}
STATUSES = {
	"FINISHED": "Finished",
	"RELEASING": "Releasing",
	"NOT_YET_RELEASED": "Not yet released",
	"CANCELLED": "Cancelled",
	"HIATUS": "On hiatus",
}
KITSU_STATUSES = {"current": "Releasing", "finished": "Finished", "upcoming": "Not yet released", "unreleased": "Not yet released", "tba": "Not yet released"}
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
# words in a wikipedia short description that mean the page is about a character
CHARACTER_WORDS = ("character", "superhero", "supervillain", "villain", "protagonist", "antagonist", "mascot", "pokémon")

class LookupFailed(Exception):
	"""Something the user should be told"""

class AniListBusy(LookupFailed):
	"""AniList's per-minute limit ran out"""

def session() -> aiohttp.ClientSession:
	return aiohttp.ClientSession(timeout=TIMEOUT, headers=HEADERS)

async def _get(session: aiohttp.ClientSession, service: str, url: str, **params):
	try:
		async with session.get(url, params=params) as resp:
			if resp.status == 200:
				return await resp.json(content_type=None)
			problem = resp.status
	except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
		problem = error
	raise LookupFailed(f"{service} isn't answering right now ({problem}).")

async def _optional(request):
	"""Extra details a card can go without."""
	try:
		return await request
	except (LookupFailed, roblox.RobloxError):
		return None

####### =================================================================== #######

def clean(text: str | None, limit: int = DESCRIPTION_LENGTH) -> str | None:
	"""Turn an api's html or markdown blurb into plain discord text that fits."""
	if not text:
		return None
	text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
	text = html.unescape(re.sub(r"<[^>]+>", "", text))
	text = re.sub(r"~!(.+?)!~", r"||\1||", text, flags=re.S)
	text = re.sub(r"__(.+?)__", r"**\1**", text)
	text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
	if len(text) > limit:
		text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "..."
		# a cut can land inside a spoiler or bold, which would leak the rest of the message
		if text.count("||") % 2:
			text += "||"
		if text.count("**") % 2:
			text += "**"
	return text or None

def _shares_word(query: str, title: str) -> bool:
	words = set(re.findall(r"\w+", title.casefold()))
	return any(word in words for word in re.findall(r"\w+", query.casefold()))

def _matches(have: str | None, wanted: str | None) -> bool:
	return not wanted or wanted.casefold() in (have or "").casefold()

def _resembles(have: str | None, wanted: str) -> bool:
	"""Close enough to be a typo of what was asked for, rather than just the nearest thing."""
	return _shares_word(wanted, have or "") or SequenceMatcher(None, (have or "").casefold(), wanted.casefold()).ratio() >= 0.75

def _exact(have: str | None, wanted: str) -> bool:
	return (have or "").casefold() == wanted.casefold()

def _date(year: int | None, month: int | None = None, day: int | None = None) -> str | None:
	if not year:
		return None
	if not month:
		return str(year)
	if not day:
		return f"{MONTHS[month - 1]} {year}"
	return f"{MONTHS[month - 1]} {day}, {year}"

def _iso_date(text: str | None) -> str | None:
	try:
		released = datetime.strptime(text or "", "%Y-%m-%d")
	except ValueError:
		return None
	return _date(released.year, released.month, released.day)

def _duration(seconds: int) -> str:
	hours, rest = divmod(seconds, 3600)
	if hours:
		return f"{hours}h {rest // 60}m"
	return f"{rest // 60}:{rest % 60:02d}"

def _card(title: str, url: str, source: str | None, **fields) -> dict:
	return {
		"title": title, "url": url, "source": source,
		"icon": None, "badge": None, "subtitle": None, "description": None, "thumbnail": None, "facts_thumbnail": None, "banner": None,
		"color": None, "facts": [], "extra": None, "links": [], "files": [],
		**fields,
	}

####### =================================================================== #######

async def roblox_user(session: aiohttp.ClientSession, username: str) -> dict:
	"""A Roblox account's public profile."""
	roblox_id, _ = await roblox.user_id(session, username)
	user = await roblox.profile(session, roblox_id)

	name = user.get("name") or username
	display = user.get("displayName") or name
	subtitle = f"@{name}" if display != name else None

	facts = []
	if user.get("isBanned"):
		facts.append(f"{theme.ERR} **This account is banned.**")
	created = user.get("created")
	if created:
		# roblox trims fractional seconds to any length which older fromisoformat rejects
		joined = datetime.fromisoformat(created[:19]).replace(tzinfo=timezone.utc)
		facts.append(f"**Joined:** {discord.utils.format_dt(joined, style='D')} ({discord.utils.format_dt(joined, style='R')})")
	counts = [(label, user.get(key)) for label, key in (("Friends", "friends"), ("Followers", "followers"), ("Following", "following"))]
	shown = [f"**{label}:** {value:,}" for label, value in counts if value is not None]
	if shown:
		facts.append(" • ".join(shown))
	if user.get("presence") == roblox.IN_GAME and user.get("location"):
		facts.append(f"**Playing:** {user['location']}")
	past = user.get("past_names") or []
	if past:
		more = f" *+{len(past) - 30} more*" if len(past) > 30 else ""
		facts.append(f"**Past usernames:** {', '.join(past[:30])}{more}")
	facts.append(f"-# ID {roblox_id}")

	return _card(
		display, roblox.profile_url(roblox_id), None, icon=PRESENCE.get(user.get("presence")),
		badge=theme.ROBLOX_VERIFIED if user.get("hasVerifiedBadge") else None,
		subtitle=subtitle, description=clean(user.get("description"), 300),
		thumbnail=user.get("headshot") or user.get("avatar"),
		facts_thumbnail=user.get("avatar") if user.get("headshot") else None,
		facts=facts, links=[("Profile", roblox.profile_url(roblox_id))],
	)

####### =================================================================== #######

async def _deezer(session: aiohttp.ClientSession, path: str, **params):
	data = await _get(session, "Deezer", f"{DEEZER}{path}", **params)
	# deezer reports its own failures with a 200
	if isinstance(data, dict) and "error" in data:
		raise LookupFailed("Deezer isn't answering right now.")
	return data

def _rym(term: str, kind: str) -> str:
	return f"https://rateyourmusic.com/search?searchterm={quote_plus(term)}&searchtype={kind}"

def _explicit(flag: bool | None) -> str:
	return f"{theme.EXPLICIT} " if flag else ""

def _music_missing(song: str | None, album: str | None, artist: str | None, searched: list[str]) -> str:
	if song:
		what = f"**{song}**" + (f" by **{artist}**" if artist else "") + (f" on **{album}**" if album else "")
	elif album:
		what = f"the album **{album}**" + (f" by **{artist}**" if artist else "")
	else:
		what = f"an artist called **{artist}**"
	return f"I couldn't find {what} on {' or '.join(searched)}."

async def music(session: aiohttp.ClientSession, song: str | None, album: str | None, artist: str | None) -> dict:
	"""A song, album or artist, whichever is the most specific thing asked for."""
	sources = [("Spotify", _spotify_music)] if SPOTIFY_ID and SPOTIFY_SECRET else []
	sources.append(("Deezer", _deezer_music))

	searched, failure, fallback = [], None, None
	for service, find in sources:
		try:
			found = await find(session, song, album, artist)
		except LookupFailed as error:
			failure = failure or error
			continue
		searched.append(service)
		if found is None:
			continue
		card, exact = found
		if exact:
			return card
		# a loose match only wins when no other site has one that fits everything asked for
		fallback = fallback or card
	if fallback is not None:
		return fallback
	if not searched:
		raise failure
	raise LookupFailed(_music_missing(song, album, artist, searched))

####### =================================================================== #######

# (token, when it expires), shared by every lookup until it runs out
_spotify_token = ("", 0.0)

async def _spotify(session: aiohttp.ClientSession, path: str, **params):
	global _spotify_token
	token, expires = _spotify_token
	try:
		if expires <= time.monotonic():
			auth = aiohttp.BasicAuth(SPOTIFY_ID, SPOTIFY_SECRET)
			async with session.post(SPOTIFY_AUTH, data={"grant_type": "client_credentials"}, auth=auth) as resp:
				if resp.status != 200:
					raise LookupFailed(f"Spotify isn't answering right now ({resp.status}).")
				data = await resp.json(content_type=None)
			token = data["access_token"]
			_spotify_token = (token, time.monotonic() + (data.get("expires_in") or 3600) - 60)

		async with session.get(f"{SPOTIFY}{path}", params=params, headers={"Authorization": f"Bearer {token}"}) as resp:
			if resp.status != 200:
				raise LookupFailed(f"Spotify isn't answering right now ({resp.status}).")
			return await resp.json(content_type=None)
	except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as error:
		raise LookupFailed(f"Spotify isn't answering right now ({error}).") from error

def _spotify_query(**fields) -> str:
	# a quote in someone's search would end spotify's field early
	return " ".join(f'{field}:"{value.replace(chr(34), "")}"' for field, value in fields.items() if value)

def _spotify_date(record: dict) -> str | None:
	parts = [int(part) for part in (record.get("release_date") or "").split("-") if part.isdigit()]
	return _date(*parts[:3]) if parts else None

def _spotify_artists(people: list[dict]) -> str:
	return ", ".join(f"[{person.get('name')}]({(person.get('external_urls') or {}).get('spotify')})" for person in people)

def _spotify_image(item: dict) -> str | None:
	images = item.get("images") or []
	return images[0].get("url") if images else None

async def _spotify_music(session: aiohttp.ClientSession, song: str | None, album: str | None, artist: str | None) -> tuple[dict, bool] | None:
	if song:
		return await _spotify_song(session, song, album, artist)
	if album:
		return await _spotify_album(session, album, artist)
	return await _spotify_artist(session, artist)

async def _spotify_song(session: aiohttp.ClientSession, song: str, album: str | None, artist: str | None) -> tuple[dict, bool] | None:
	query = _spotify_query(track=song, artist=artist, album=album)
	rows = ((await _spotify(session, "/search", q=query, type="track", limit=10)).get("tracks") or {}).get("items") or []
	if not rows:
		loose = " ".join(part for part in (song, artist, album) if part)
		rows = ((await _spotify(session, "/search", q=loose, type="track", limit=10)).get("tracks") or {}).get("items") or []
	fits = [row for row in rows if row and _matches(row.get("name"), song)]
	if not fits:
		return None

	def fitting(row: dict) -> bool:
		people = " / ".join(person.get("name") or "" for person in row.get("artists") or [])
		return _matches(people, artist) and _matches((row.get("album") or {}).get("name"), album)
	track = max(fits, key=lambda row: _exact(row.get("name"), song) + fitting(row))

	record = track.get("album") or {}
	url = (track.get("external_urls") or {}).get("spotify")
	people = track.get("artists") or []
	facts = [
		f"**{'Artists' if len(people) > 1 else 'Artist'}:** {_spotify_artists(people)}",
		f"**Album:** [{record.get('name')}]({(record.get('external_urls') or {}).get('spotify')})",
	]
	released = _spotify_date(record)
	if released:
		facts.append(f"**Released:** {released}")
	facts.append(f"**Length:** {_duration((track.get('duration_ms') or 0) // 1000)}")

	lead = people[0].get("name") if people else ""
	card = _card(
		track.get("name") or song, url, "Spotify",
		subtitle=f"{_explicit(track.get('explicit'))}Song by {', '.join(person.get('name') for person in people)}",
		thumbnail=_spotify_image(record), facts=facts,
		links=[("Spotify", url), ("RateYourMusic", _rym(f"{lead} {record.get('name')}", "l"))],
	)
	return card, fitting(track)

async def _spotify_album(session: aiohttp.ClientSession, album: str, artist: str | None) -> tuple[dict, bool] | None:
	query = _spotify_query(album=album, artist=artist)
	rows = ((await _spotify(session, "/search", q=query, type="album", limit=10)).get("albums") or {}).get("items") or []
	fits = [row for row in rows if row and _matches(row.get("name"), album)]
	if not fits:
		return None

	def fitting(row: dict) -> bool:
		return _matches(" / ".join(person.get("name") or "" for person in row.get("artists") or []), artist)
	best = max(fits, key=lambda row: _exact(row.get("name"), album) + fitting(row))

	record = await _spotify(session, f"/albums/{best['id']}")
	url = (record.get("external_urls") or {}).get("spotify")
	people = record.get("artists") or []
	tracks = (record.get("tracks") or {}).get("items") or []
	facts = [f"**{'Artists' if len(people) > 1 else 'Artist'}:** {_spotify_artists(people)}"]
	released = _spotify_date(record)
	if released:
		facts.append(f"**Released:** {released}")
	length = sum(row.get("duration_ms") or 0 for row in tracks) // 1000
	facts.append(f"**Tracks:** {record.get('total_tracks') or len(tracks)} ({_duration(length)})")
	if record.get("genres"):
		facts.append(f"**Genres:** {', '.join(record['genres'])}")
	if record.get("label"):
		facts.append(f"**Label:** {record['label']}")

	tracklist = [
		f"`{i:>2}` {row.get('name')} {_explicit(row.get('explicit'))}• {_duration((row.get('duration_ms') or 0) // 1000)}"
		for i, row in enumerate(tracks[:12], 1)
	]
	if len(tracks) > 12:
		tracklist.append(f"-# +{len(tracks) - 12} more")

	lead = people[0].get("name") if people else ""
	card = _card(
		record.get("name") or album, url, "Spotify",
		subtitle=f"{(record.get('album_type') or 'album').title()} by {', '.join(person.get('name') for person in people)}",
		thumbnail=_spotify_image(record), facts=facts,
		extra="**Tracklist**\n" + "\n".join(tracklist) if tracklist else None,
		links=[("Spotify", url), ("RateYourMusic", _rym(f"{lead} {record.get('name')}", "l"))],
	)
	return card, fitting(best)

async def _spotify_artist(session: aiohttp.ClientSession, artist: str) -> tuple[dict, bool] | None:
	rows = ((await _spotify(session, "/search", q=artist, type="artist", limit=10)).get("artists") or {}).get("items") or []
	person_id = next((row["id"] for row in rows if row and _exact(row.get("name"), artist)), None)
	if person_id is None:
		# artist search leans on popularity and leaves small artists out, but their songs still show up
		songs = ((await _spotify(session, "/search", q=_spotify_query(artist=artist), type="track", limit=10)).get("tracks") or {}).get("items") or []
		credited = [person for row in songs if row for person in row.get("artists") or []]
		person_id = next((person["id"] for person in credited if _exact(person.get("name"), artist)), None)
	exact = person_id is not None
	if person_id is None:
		person_id = next((row["id"] for row in rows if row and _resembles(row.get("name"), artist)), None)
		if person_id is None:
			return None

	person, top = await asyncio.gather(
		_spotify(session, f"/artists/{person_id}"),
		_optional(_spotify(session, f"/artists/{person_id}/top-tracks", market="US")),
	)
	url = (person.get("external_urls") or {}).get("spotify")
	facts = [f"**Followers:** {(person.get('followers') or {}).get('total') or 0:,}"]
	if person.get("genres"):
		facts.append(f"**Genres:** {', '.join(person['genres'][:5])}")
	songs = [
		f"{i}. [{row.get('name')}]({(row.get('external_urls') or {}).get('spotify')})"
		for i, row in enumerate(((top or {}).get("tracks") or [])[:5], 1)
	]

	card = _card(
		person.get("name") or artist, url, "Spotify",
		subtitle="Artist", thumbnail=_spotify_image(person), facts=facts,
		extra="**Top songs**\n" + "\n".join(songs) if songs else None,
		links=[("Spotify", url), ("RateYourMusic", _rym(person.get("name") or artist, "a"))],
	)
	return card, exact

####### =================================================================== #######

async def _deezer_music(session: aiohttp.ClientSession, song: str | None, album: str | None, artist: str | None) -> tuple[dict, bool] | None:
	if song:
		return await _deezer_song(session, song, album, artist)
	if album:
		return await _deezer_album(session, album, artist)
	return await _deezer_artist(session, artist)

async def _deezer_song(session: aiohttp.ClientSession, song: str, album: str | None, artist: str | None) -> tuple[dict, bool] | None:
	# deezer's field search finds nothing, so search loosely and pick the match here
	query = " ".join(part for part in (song, artist, album) if part)
	rows = (await _deezer(session, "/search/track", q=query, limit=25)).get("data") or []
	fits = [row for row in rows if _matches(row.get("title"), song)]
	if not fits:
		return None

	def fitting(row: dict) -> bool:
		return _matches((row.get("artist") or {}).get("name"), artist) and _matches((row.get("album") or {}).get("title"), album)
	best = max(fits, key=lambda row: _exact(row.get("title_short"), song) + fitting(row))

	track = await _deezer(session, f"/track/{best['id']}")
	track_artist = track.get("artist") or {}
	track_album = track.get("album") or {}
	facts = [
		f"**Artist:** [{track_artist.get('name')}]({track_artist.get('link')})",
		f"**Album:** [{track_album.get('title')}](https://www.deezer.com/album/{track_album.get('id')})",
	]
	released = _iso_date(track.get("release_date"))
	if released:
		facts.append(f"**Released:** {released}")
	facts.append(f"**Length:** {_duration(track.get('duration') or 0)}")
	if track.get("bpm"):
		facts.append(f"**BPM:** {round(track['bpm'])}")

	links = [
		("Deezer", track.get("link")),
		("RateYourMusic", _rym(f"{track_artist.get('name')} {track_album.get('title')}", "l")),
	]
	card = _card(
		track.get("title") or song, track.get("link"), "Deezer",
		subtitle=f"{_explicit(track.get('explicit_lyrics'))}Song by {track_artist.get('name')}",
		thumbnail=track_album.get("cover_xl"), facts=facts, links=links,
	)
	return card, fitting(best)

async def _deezer_album(session: aiohttp.ClientSession, album: str, artist: str | None) -> tuple[dict, bool] | None:
	query = " ".join(part for part in (album, artist) if part)
	rows = (await _deezer(session, "/search/album", q=query, limit=25)).get("data") or []
	fits = [row for row in rows if _matches(row.get("title"), album)]
	if not fits:
		return None

	def fitting(row: dict) -> bool:
		return _matches((row.get("artist") or {}).get("name"), artist)
	best = max(fits, key=lambda row: _exact(row.get("title"), album) + fitting(row))

	record = await _deezer(session, f"/album/{best['id']}")
	record_artist = record.get("artist") or {}
	kind = "EP" if record.get("record_type") == "ep" else (record.get("record_type") or "album").title()
	facts = [f"**Artist:** [{record_artist.get('name')}](https://www.deezer.com/artist/{record_artist.get('id')})"]
	released = _iso_date(record.get("release_date"))
	if released:
		facts.append(f"**Released:** {released}")
	facts.append(f"**Tracks:** {record.get('nb_tracks') or 0} ({_duration(record.get('duration') or 0)})")
	genres = [genre["name"] for genre in (record.get("genres") or {}).get("data") or [] if genre.get("name")]
	if genres:
		facts.append(f"**Genres:** {', '.join(genres)}")
	if record.get("label"):
		facts.append(f"**Label:** {record['label']}")
	if record.get("fans"):
		facts.append(f"**Fans on Deezer:** {record['fans']:,}")

	tracks = (record.get("tracks") or {}).get("data") or []
	tracklist = [
		f"`{i:>2}` {row.get('title')} {_explicit(row.get('explicit_lyrics'))}• {_duration(row.get('duration') or 0)}"
		for i, row in enumerate(tracks[:12], 1)
	]
	if len(tracks) > 12:
		tracklist.append(f"-# +{len(tracks) - 12} more")

	card = _card(
		record.get("title") or album, record.get("link"), "Deezer",
		subtitle=f"{kind} by {record_artist.get('name')}",
		thumbnail=record.get("cover_xl"), facts=facts,
		extra="**Tracklist**\n" + "\n".join(tracklist) if tracklist else None,
		links=[("Deezer", record.get("link")), ("RateYourMusic", _rym(f"{record_artist.get('name')} {record.get('title')}", "l"))],
	)
	return card, fitting(best)

async def _deezer_artist(session: aiohttp.ClientSession, artist: str) -> tuple[dict, bool] | None:
	rows = (await _deezer(session, "/search/artist", q=artist, limit=10)).get("data") or []
	best = next((row for row in rows if _exact(row.get("name"), artist)), None)
	best = best or next((row for row in rows if _resembles(row.get("name"), artist)), None)
	if best is None:
		return None

	person, top = await asyncio.gather(
		_deezer(session, f"/artist/{best['id']}"),
		_optional(_deezer(session, f"/artist/{best['id']}/top", limit=5)),
	)
	facts = [
		f"**Albums:** {person.get('nb_album') or 0:,}",
		f"**Fans on Deezer:** {person.get('nb_fan') or 0:,}",
	]
	songs = [f"{i}. [{row.get('title')}]({row.get('link')})" for i, row in enumerate((top or {}).get("data") or [], 1)]

	card = _card(
		person.get("name") or artist, person.get("link"), "Deezer",
		subtitle="Artist", thumbnail=person.get("picture_xl"), facts=facts,
		extra="**Top songs**\n" + "\n".join(songs) if songs else None,
		links=[("Deezer", person.get("link")), ("RateYourMusic", _rym(person.get("name") or artist, "a"))],
	)
	return card, _exact(best.get("name"), artist)

####### =================================================================== #######

def steamdb_rating(positive: int, total: int) -> float | None:
	"""SteamDB's rating, which pulls games with few reviews towards 50%."""
	if not total:
		return None
	average = positive / total
	return average - (average - 0.5) * 2 ** -math.log10(total + 1)

async def _steam_app_id(session: aiohttp.ClientSession, name: str) -> int:
	found = STEAM_APP_IN_LINK.search(name)
	if found:
		return int(found.group(1))
	data = await _get(session, "Steam", "https://store.steampowered.com/api/storesearch/", term=name, cc="us", l="english")
	items = data.get("items") or []
	if not items:
		raise LookupFailed(f"I couldn't find a Steam game called **{name}**.")
	# steam ranks by relevance, which can put a sequel ahead of an exact name
	return next((item for item in items if item.get("name", "").casefold() == name.casefold()), items[0])["id"]

async def _steam_header(session: aiohttp.ClientSession, header: str | None) -> str | None:
	"""The header at twice the size, which older games don't have."""
	if not header:
		return None
	bigger = header.replace("/header.jpg", "/header_2x.jpg")
	try:
		async with session.head(bigger) as resp:
			if resp.status == 200:
				return bigger
	except (aiohttp.ClientError, asyncio.TimeoutError):
		pass
	return header

async def game(session: aiohttp.ClientSession, name: str) -> dict:
	"""A Steam game's store page, with player counts and review stats."""
	app_id = await _steam_app_id(session, name)
	details, players, reviews, spy = await asyncio.gather(
		_get(session, "Steam", "https://store.steampowered.com/api/appdetails", appids=app_id, cc="us", l="english"),
		_optional(_get(session, "Steam", "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/", appid=app_id)),
		_optional(_get(
			session, "Steam", f"https://store.steampowered.com/appreviews/{app_id}",
			json=1, language="all", purchase_type="all", num_per_page=0,
		)),
		_optional(_get(session, "SteamSpy", "https://steamspy.com/api.php", request="appdetails", appid=app_id)),
	)
	entry = (details or {}).get(str(app_id)) or {}
	if not entry.get("success"):
		raise LookupFailed("I couldn't find that game on Steam.")
	app = entry["data"]
	store_url = f"https://store.steampowered.com/app/{app_id}"

	facts = []
	if app.get("is_free"):
		facts.append("**Price:** Free")
	elif app.get("price_overview"):
		price = app["price_overview"]
		sale = f" ~~{price['initial_formatted']}~~ (-{price['discount_percent']}%)" if price.get("discount_percent") else ""
		facts.append(f"**Price:** {price.get('final_formatted')}{sale}")
	release = app.get("release_date") or {}
	if release.get("date"):
		facts.append(f"**{'Releases' if release.get('coming_soon') else 'Released'}:** {release['date']}")

	playing = ((players or {}).get("response") or {}).get("player_count")
	peak = (spy or {}).get("ccu")
	if playing is not None:
		facts.append(f"**Playing now:** {playing:,}" + (f" (peaked at {peak:,} yesterday)" if peak else ""))

	summary = (reviews or {}).get("query_summary") or {}
	total = summary.get("total_reviews") or 0
	if total:
		positive = summary.get("total_positive") or 0
		facts.append(f"**Reviews:** {summary.get('review_score_desc')} ({positive / total:.0%} of {total:,})")
		facts.append(f"**SteamDB rating:** {steamdb_rating(positive, total):.1%}")
	if (app.get("metacritic") or {}).get("score"):
		facts.append(f"**Metacritic:** {app['metacritic']['score']}")
	owners = (spy or {}).get("owners")
	if owners:
		facts.append(f"**Owners:** ~{owners.replace(' .. ', ' to ~')}")

	tags = list(((spy or {}).get("tags") or {}).keys())[:6] if isinstance((spy or {}).get("tags"), dict) else []
	genres = tags or [genre["description"] for genre in app.get("genres") or [] if genre.get("description")]
	if genres:
		facts.append(f"**{'Tags' if tags else 'Genres'}:** {', '.join(genres)}")

	makers = ", ".join(app.get("developers") or [])
	return _card(
		app.get("name") or name, store_url, "Steam, SteamSpy",
		subtitle=f"> By {makers}" if makers else None,
		description=clean(app.get("short_description")),
		banner=await _steam_header(session, app.get("header_image")), facts=facts,
		links=[("Steam", store_url), ("SteamDB", f"https://steamdb.info/app/{app_id}/charts/")],
	)

####### =================================================================== #######

# query and variables -> (when it goes stale, answer), since anilist only allows 30 requests a minute
_anilist_cache: dict[str, tuple[float, dict | None]] = {}

async def _anilist(session: aiohttp.ClientSession, query: str, variables: dict) -> dict | None:
	"""Ask AniList, None when nothing matched."""
	key = json.dumps([query, variables], sort_keys=True)
	cached = _anilist_cache.get(key)
	if cached and cached[0] > time.monotonic():
		return cached[1]

	try:
		async with session.post(ANILIST, json={"query": query, "variables": variables}) as resp:
			if resp.status == 429:
				raise AniListBusy("AniList is getting too many requests, try again in a minute.")
			if resp.status not in (200, 404):
				raise LookupFailed(f"AniList isn't answering right now ({resp.status}).")
			data = None if resp.status == 404 else (await resp.json(content_type=None)).get("data")
	except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
		raise LookupFailed(f"AniList isn't answering right now ({error}).") from error

	_anilist_cache.pop(key, None)
	_anilist_cache[key] = (time.monotonic() + ANILIST_CACHE_SECONDS, data)
	if len(_anilist_cache) > ANILIST_CACHE_SIZE:
		del _anilist_cache[next(iter(_anilist_cache))]
	return data

MEDIA_QUERY = """
query ($search: String, $type: MediaType, $id: Int, $idMal: Int) {
	Media(search: $search, id: $id, idMal: $idMal, type: $type, isAdult: false) {
		siteUrl idMal format status episodes duration chapters volumes source
		title { romaji english native }
		startDate { year month day }
		endDate { year month day }
		season seasonYear averageScore popularity favourites genres
		rankings { rank type allTime }
		description
		coverImage { extraLarge color }
		bannerImage
		nextAiringEpisode { episode airingAt }
		studios(isMain: true) { nodes { name siteUrl } }
		staff(perPage: 6, sort: RELEVANCE) { edges { role node { name { full } siteUrl } } }
	}
}
"""

async def _kitsu(session: aiohttp.ClientSession, search: str, kind: str) -> dict | None:
	"""Kitsu's typo-tolerant search, with the other sites' IDs and genres attached."""
	data = await _get(
		session, "Kitsu", f"{KITSU}/{kind.lower()}",
		**{
			"filter[text]": search, "page[limit]": 1, "include": "mappings,categories",
			"fields[mappings]": "externalSite,externalId", "fields[categories]": "title",
		},
	)
	rows = data.get("data") or []
	if not rows:
		return None
	included = data.get("included") or []
	return {
		**rows[0]["attributes"],
		"ids": {
			row["attributes"].get("externalSite"): row["attributes"].get("externalId")
			for row in included if row.get("type") == "mappings"
		},
		"genres": [row["attributes"].get("title") for row in included if row.get("type") == "categories"],
	}

def _kitsu_anilist_ids(entry: dict, kind: str) -> dict | None:
	for site, key in ((f"anilist/{kind.lower()}", "id"), (f"myanimelist/{kind.lower()}", "idMal")):
		if str(entry["ids"].get(site) or "").isdigit():
			return {key: int(entry["ids"][site])}
	return None

def _kitsu_card(entry: dict, kind: str) -> dict:
	"""Stands in for the AniList card while AniList is turning us away."""
	titles = entry.get("titles") or {}
	title = titles.get("en") or entry.get("canonicalTitle") or "Unknown"
	others = [t for t in (titles.get("en_jp"), titles.get("ja_jp")) if t and t != title]

	facts = []
	subtype = entry.get("subtype") or ""
	status = KITSU_STATUSES.get(entry.get("status") or "", "")
	if kind == "ANIME" and status == "Releasing":
		status = "Airing"
	facts.append(" • ".join(part for part in (FORMATS.get(subtype.upper(), subtype.title()), status) if part))
	if kind == "ANIME" and entry.get("episodeCount"):
		each = " each" if entry["episodeCount"] > 1 else ""
		length = f" ({entry['episodeLength']} min{each})" if entry.get("episodeLength") else ""
		facts.append(f"**Episodes:** {entry['episodeCount']}{length}")
	if kind == "MANGA":
		counts = [f"{entry[key]} {label}" for key, label in (("chapterCount", "chapters"), ("volumeCount", "volumes")) if entry.get(key)]
		if counts:
			facts.append(f"**Length:** {', '.join(counts)}")
	start = _iso_date(entry.get("startDate"))
	if start:
		end = _iso_date(entry.get("endDate"))
		span = f"{start} to {end}" if end and end != start else f"{start} to now" if entry.get("status") == "current" else start
		facts.append(f"**{'Aired' if kind == 'ANIME' else 'Published'}:** {span}")
	if entry.get("averageRating"):
		rank = f" (#{entry['ratingRank']} all time)" if entry.get("ratingRank") else ""
		facts.append(f"**Score:** {float(entry['averageRating']):.0f}%{rank}")
	facts.append(f"**Members:** {int(entry.get('userCount') or 0):,} • **Favourites:** {int(entry.get('favoritesCount') or 0):,}")
	if entry["genres"]:
		facts.append(f"**Genres:** {', '.join(entry['genres'][:6])}")

	url = f"https://kitsu.app/{kind.lower()}/{entry.get('slug')}"
	links = [("Kitsu", url)]
	ids = entry["ids"]
	if str(ids.get(f"anilist/{kind.lower()}") or "").isdigit():
		links.append(("AniList", f"https://anilist.co/{kind.lower()}/{ids[f'anilist/{kind.lower()}']}"))
	if str(ids.get(f"myanimelist/{kind.lower()}") or "").isdigit():
		links.append(("MyAnimeList", f"https://myanimelist.net/{kind.lower()}/{ids[f'myanimelist/{kind.lower()}']}"))
	return _card(
		title, url, "Kitsu",
		subtitle=" • ".join(others) or None, description=clean(entry.get("synopsis")),
		thumbnail=(entry.get("posterImage") or {}).get("large"),
		banner=(entry.get("coverImage") or {}).get("large"),
		facts=facts, links=links,
	)

async def media(session: aiohttp.ClientSession, name: str, kind: str) -> dict:
	"""An anime or manga from AniList. kind is ANIME or MANGA."""
	search = SEASON_SHORTHAND.sub(r"season \1", name)
	try:
		data = await _anilist(session, MEDIA_QUERY, {"search": search, "type": kind})
		item = (data or {}).get("Media")
		if not item:
			# anilist needs every word to match, so for example "rezero" or a typo finds nothing there
			entry = await _optional(_kitsu(session, search, kind))
			ids = _kitsu_anilist_ids(entry, kind) if entry else None
			if ids:
				data = await _anilist(session, MEDIA_QUERY, {**ids, "type": kind})
				item = (data or {}).get("Media")
	except AniListBusy:
		entry = await _optional(_kitsu(session, search, kind))
		if entry is None or entry.get("nsfw"):
			raise
		return _kitsu_card(entry, kind)
	if not item:
		raise LookupFailed(f"I couldn't find {'an anime' if kind == 'ANIME' else 'a manga'} called **{name}**.")

	titles = item.get("title") or {}
	title = titles.get("english") or titles.get("romaji") or name
	others = [t for t in (titles.get("romaji"), titles.get("native")) if t and t != title]

	facts = []
	form = FORMATS.get(item.get("format") or "", (item.get("format") or "").replace("_", " ").title())
	status = STATUSES.get(item.get("status") or "", "")
	if kind == "ANIME" and item.get("status") == "RELEASING":
		status = "Airing"
	facts.append(" • ".join(part for part in (form, status) if part))

	if kind == "ANIME":
		if item.get("episodes"):
			each = " each" if item["episodes"] > 1 else ""
			length = f" ({item['duration']} min{each})" if item.get("duration") else ""
			facts.append(f"**Episodes:** {item['episodes']}{length}")
		upcoming = item.get("nextAiringEpisode")
		if upcoming:
			facts.append(f"**Episode {upcoming['episode']}:** <t:{upcoming['airingAt']}:R>")
	else:
		counts = [f"{item[key]} {label}" for key, label in (("chapters", "chapters"), ("volumes", "volumes")) if item.get(key)]
		if counts:
			facts.append(f"**Length:** {', '.join(counts)}")

	start = _date(**(item.get("startDate") or {}))
	end = _date(**(item.get("endDate") or {}))
	if start:
		span = start
		if end and end != start:
			span = f"{start} to {end}"
		elif item.get("status") == "RELEASING":
			span = f"{start} to now"
		facts.append(f"**{'Aired' if kind == 'ANIME' else 'Published'}:** {span}")
	if kind == "ANIME" and item.get("season") and item.get("seasonYear"):
		facts.append(f"**Season:** {item['season'].title()} {item['seasonYear']}")

	if item.get("averageScore"):
		ranked = next((r for r in item.get("rankings") or [] if r.get("type") == "RATED" and r.get("allTime")), None)
		facts.append(f"**Score:** {item['averageScore']}%" + (f" (#{ranked['rank']} all time)" if ranked else ""))
	facts.append(f"**Members:** {item.get('popularity') or 0:,} • **Favourites:** {item.get('favourites') or 0:,}")

	if kind == "ANIME":
		studios = [f"[{s['name']}]({s['siteUrl']})" for s in (item.get("studios") or {}).get("nodes") or []]
		if studios:
			facts.append(f"**Studio:** {', '.join(studios)}")
	else:
		creators = [
			f"[{edge['node']['name']['full']}]({edge['node']['siteUrl']}) ({edge['role']})"
			for edge in (item.get("staff") or {}).get("edges") or []
			if any(word in (edge.get("role") or "") for word in ("Story", "Art"))
		]
		if creators:
			facts.append(f"**By:** {', '.join(creators[:2])}")
	if item.get("genres"):
		facts.append(f"**Genres:** {', '.join(item['genres'])}")

	links = [("AniList", item["siteUrl"])]
	if item.get("idMal"):
		links.append(("MyAnimeList", f"https://myanimelist.net/{kind.lower()}/{item['idMal']}"))
	cover = item.get("coverImage") or {}
	return _card(
		title, item["siteUrl"], "AniList",
		subtitle=" • ".join(others) or None, description=clean(item.get("description")),
		thumbnail=cover.get("extraLarge"), banner=item.get("bannerImage"),
		color=discord.Color.from_str(cover["color"]) if cover.get("color") else None,
		facts=facts, links=links,
	)

####### =================================================================== #######

CHARACTER_QUERY = """
query ($search: String) {
	Character(search: $search) {
		siteUrl favourites gender age
		name { full native alternative }
		dateOfBirth { month day }
		image { large }
		description
		media(perPage: 3, sort: POPULARITY_DESC) { nodes { siteUrl isAdult title { romaji english } } }
	}
}
"""

async def _wikipedia_character(session: aiohttp.ClientSession, search: str, name: str) -> dict | None:
	"""The first wikipedia result whose short description says it's a character."""
	data = await _get(
		session, "Wikipedia", WIKIPEDIA,
		action="query", format="json", formatversion=2, redirects=1,
		generator="search", gsrsearch=search, gsrlimit=8,
		prop="description|pageimages|extracts|info|pageprops", inprop="url", ppprop="wikibase_item",
		exintro=1, explaintext=1, exsentences=3,
		piprop="thumbnail", pithumbsize=600, pilicense="any",
	)
	pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda page: page.get("index", 0))
	for page in pages:
		title = page.get("title") or ""
		blurb = (page.get("description") or "").casefold()
		if title.startswith("List of") or not _shares_word(name, title):
			continue
		if any(word in blurb for word in CHARACTER_WORDS):
			# pronunciation guides make the opening line hard to read
			extract = re.sub(r"\s*\([^()]*/[^()]*/[^()]*\)", "", page.get("extract") or "")
			return _card(
				title, page.get("fullurl"), "Wikipedia",
				subtitle=page.get("description"), description=clean(extract),
				thumbnail=(page.get("thumbnail") or {}).get("source"),
				links=[("Wikipedia", page.get("fullurl"))],
				wikidata=(page.get("pageprops") or {}).get("wikibase_item"),
			)
	return None

async def _fandom_image(session: aiohttp.ClientSession, wikidata: str) -> dict | None:
	"""The character's picture from their Fandom wiki, which Wikidata links to."""
	data = await _get(session, "Wikidata", WIKIDATA, action="wbgetclaims", format="json", entity=wikidata, property="P6262")
	articles = [(claim["mainsnak"].get("datavalue") or {}).get("value") or "" for claim in (data.get("claims") or {}).get("P6262") or []]
	# a dotted wiki like ru.harrypotter is another language, which lives at a different address
	article = next((a for a in articles if ":" in a and "." not in a.split(":", 1)[0]), None)
	if article is None:
		return None
	wiki, title = article.split(":", 1)

	data = await _get(
		session, "Fandom", f"https://{wiki}.fandom.com/api.php",
		action="query", format="json", formatversion=2, redirects=1,
		titles=title.replace("_", " "), prop="pageimages", piprop="original",
	)
	pages = (data.get("query") or {}).get("pages") or [{}]
	source = (pages[0].get("original") or {}).get("source")
	if not source:
		return None

	# fandom's image cdn challenges anything without a fandom referer, so discord can't load it and it gets attached instead
	scaled = source.replace("/revision/latest", "/revision/latest/scale-to-width-down/600", 1)
	try:
		async with session.get(scaled, headers={"Referer": f"https://{wiki}.fandom.com/"}) as resp:
			extension = IMAGE_TYPES.get(resp.content_type)
			if resp.status != 200 or extension is None:
				return None
			image = await resp.read()
	except (aiohttp.ClientError, asyncio.TimeoutError):
		return None
	return {"file": (f"character.{extension}", image), "url": f"https://{wiki}.fandom.com/wiki/{quote(title)}"}

async def _anilist_character(session: aiohttp.ClientSession, name: str) -> dict | None:
	data = await _anilist(session, CHARACTER_QUERY, {"search": name})
	person = (data or {}).get("Character")
	if not person:
		return None
	names = person.get("name") or {}
	full = names.get("full") or name
	if not _shares_word(name, " ".join([full, *(names.get("alternative") or [])])):
		return None

	facts = []
	works = [
		f"[{node['title'].get('english') or node['title'].get('romaji')}]({node['siteUrl']})"
		for node in (person.get("media") or {}).get("nodes") or [] if not node.get("isAdult")
	]
	if works:
		facts.append(f"**Appears in:** {', '.join(works)}")
	if person.get("gender"):
		facts.append(f"**Gender:** {person['gender']}")
	if person.get("age"):
		facts.append(f"**Age:** {person['age']}")
	born = person.get("dateOfBirth") or {}
	if born.get("month") and born.get("day"):
		facts.append(f"**Birthday:** {MONTHS[born['month'] - 1]} {born['day']}")
	facts.append(f"**Favourites:** {person.get('favourites') or 0:,}")
	aliases = [alias for alias in names.get("alternative") or [] if alias][:4]
	if aliases:
		facts.append(f"**Also known as:** {', '.join(aliases)}")

	return _card(
		full, person["siteUrl"], "AniList",
		subtitle=names.get("native"), description=clean(person.get("description")),
		thumbnail=(person.get("image") or {}).get("large"), facts=facts,
		links=[("AniList", person["siteUrl"])],
	)

async def character(session: aiohttp.ClientSession, name: str) -> dict:
	"""A fictional character, from Wikipedia first and AniList for the ones it doesn't cover."""
	# the plain name finds most pages, the suffix finds ones like link that share a common word
	found = await asyncio.gather(
		_optional(_wikipedia_character(session, name, name)),
		_optional(_wikipedia_character(session, f"{name} fictional character", name)),
	)
	card = next((result for result in found if result), None)
	if card is None:
		card = await _anilist_character(session, name)
		if card is None:
			raise LookupFailed(f"I couldn't find a character called **{name}**.")
		return card

	wikidata = card.pop("wikidata", None)
	fandom = await _optional(_fandom_image(session, wikidata)) if wikidata else None
	if fandom:
		card["thumbnail"] = f"attachment://{fandom['file'][0]}"
		card["files"] = [fandom["file"]]
		card["links"].append(("Fandom", fandom["url"]))
		card["source"] = "Wikipedia, Fandom"
	return card
