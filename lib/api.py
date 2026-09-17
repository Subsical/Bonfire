import os
import time
from collections import defaultdict, deque

from aiohttp import web

from lib import database as db
from lib import polls

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
	return bool(secret) and request.headers.get("X-Bot-Secret") == secret

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

async def start(bot, port: int = 8081):
	app = web.Application()
	app["bot"] = bot
	app.add_routes([
		web.get("/guilds/{guild_id}/info", get_info),
		web.get("/guilds/{guild_id}/modules", get_modules),
		web.post("/guilds/{guild_id}/modules", set_module),
		web.delete("/guilds/{guild_id}/polls/{poll_id}", delete_poll),
	])
	runner = web.AppRunner(app)
	await runner.setup()
	await web.TCPSite(runner, "0.0.0.0", port).start()
	return runner
