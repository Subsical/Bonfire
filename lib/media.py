import asyncio
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import tempfile
from urllib.parse import urlparse

import aiohttp
import discord

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".avif", ".heic")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".gif", ".apng", ".flv", ".wmv", ".ts")
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS

MAX_SOURCE_BYTES = 100*1024*1024  # 100 MB
MAX_GIF_SECONDS = 60  # gifski holds every frame in memory

MAX_CONCURRENT = 4
MAX_QUEUED = 64

_slots = asyncio.Semaphore(MAX_CONCURRENT)
_queued = 0

def busy() -> bool:
	return _slots.locked()

def queue_full() -> bool:
	return _queued >= MAX_QUEUED

def slot():
	"""Hold this while encoding, so every converter shares the same concurrency limit."""
	return _slots

########## ======================================================================== ##########

async def run(*args: str, timeout: float = 300) -> tuple[int, bytes, bytes]:
	proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
	try:
		out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
	except asyncio.TimeoutError:
		proc.kill()
		await proc.wait()
		return -1, b"", b"timed out"
	return proc.returncode, out, err

def _read_file(path: str) -> bytes:
	with open(path, "rb") as f:
		return f.read()

def _write_file(path: str, data: bytes):
	with open(path, "wb") as f:
		f.write(data)

async def write_file(path: str, data: bytes):
	await asyncio.to_thread(_write_file, path, data)

async def read_if_fits(path: str, target_bytes: int) -> bytes | None:
	"""Read the file, but only return it if it's within the limit."""
	if not os.path.exists(path) or os.path.getsize(path) > target_bytes:
		return None
	return await asyncio.to_thread(_read_file, path) or None

def upload_limit(interaction: discord.Interaction) -> int:
	"""Attachment limit here, minus headroom for the rest of the payload."""
	limit = interaction.guild.filesize_limit if interaction.guild is not None else 10*1024*1024
	return limit-512 * 1024

########## ======================================================================== ##########

def _is_public_host(host: str) -> bool:
	"""Resolve a hostname and refuse anything pointing at a private or local address."""
	try:
		infos = socket.getaddrinfo(host, None)
	except socket.gaierror:
		return False
	for info in infos:
		ip = ipaddress.ip_address(info[4][0])
		if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
			return False
	return bool(infos)

async def _read_response(resp: "aiohttp.ClientResponse", suffix: str, max_bytes: int) -> tuple[bytes | None, str, str | None]:
	"""Validate a response's type and size, then read its body."""
	if resp.status != 200:
		return None, "", f"That link returned HTTP {resp.status}."

	if not suffix:
		content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
		suffix = mimetypes.guess_extension(content_type) or ""
		if suffix == ".jpe":
			suffix = ".jpg"
	if suffix not in MEDIA_EXTENSIONS:
		return None, "", "That link isn't an image or video I can convert."

	too_big = f"That file is too big to convert (max {max_bytes / (1024*1024):.0f}MB)."
	declared = resp.headers.get("Content-Length")
	if declared and declared.isdigit() and int(declared) > max_bytes:
		return None, "", too_big

	# read in chunks so a lying Content-Length can't blow up memory
	chunks, total = [], 0
	async for chunk in resp.content.iter_chunked(64 * 1024):
		total += len(chunk)
		if total > max_bytes:
			return None, "", too_big
		chunks.append(chunk)

	data = b"".join(chunks)
	return (data, suffix, None) if data else (None, "", "That link gave me an empty file.")

async def download(url: str, max_bytes: int) -> tuple[bytes | None, str, str | None]:
	"""Fetch a media URL. Returns (data, suffix, error)."""
	parsed = urlparse(url)
	if parsed.scheme not in ("http", "https") or not parsed.hostname:
		return None, "", "That's not a valid link!"
	if not await asyncio.to_thread(_is_public_host, parsed.hostname):
		return None, "", "I can't fetch from that address!"

	suffix = os.path.splitext(parsed.path)[1].lower()
	timeout = aiohttp.ClientTimeout(total=120, connect=15)
	try:
		async with aiohttp.ClientSession(timeout=timeout) as session:
			# follow redirects by hand so every hop gets host-checked, not just the first
			for _ in range(5):
				async with session.get(url, allow_redirects=False) as resp:
					if resp.status not in (301, 302, 303, 307, 308):
						return await _read_response(resp, suffix, max_bytes)

					url = str(resp.headers.get("Location") or "")
					parsed = urlparse(url)
					if parsed.scheme not in ("http", "https") or not parsed.hostname:
						return None, "", "That link redirects somewhere I can't follow."
					if not await asyncio.to_thread(_is_public_host, parsed.hostname):
						return None, "", "I can't fetch from that address!"
					if not suffix:
						suffix = os.path.splitext(parsed.path)[1].lower()
			return None, "", "That link redirects too many times."
	except aiohttp.ClientError:
		return None, "", "Couldn't download anything from that link."
	except asyncio.TimeoutError:
		return None, "", "That link took too long to download."

########## ======================================================================== ##########

async def probe(path: str) -> dict:
	code, out, _ = await run(
		"ffprobe", "-v", "quiet", "-print_format", "json",
		"-show_format", "-show_streams", "-select_streams", "v:0", path,
	)
	if code != 0:
		return {}
	try:
		return json.loads(out)
	except json.JSONDecodeError:
		return {}

def probe_duration(info: dict) -> float | None:
	for source in (info.get("format", {}), *(info.get("streams") or [{}])):
		try:
			duration = float(source.get("duration", ""))
		except ValueError:
			continue
		if duration > 0:
			return duration
	return None

def probe_fps(info: dict, fallback: float = 20.0) -> float:
	stream = (info.get("streams") or [{}])[0]
	for key in ("avg_frame_rate", "r_frame_rate"):
		num, _, den = stream.get(key, "").partition("/")
		try:
			fps = int(num) / int(den)
		except (ValueError, ZeroDivisionError):
			continue
		if 1 <= fps <= 60:
			return fps
	return fallback

def probe_width(info: dict) -> int | None:
	width = (info.get("streams") or [{}])[0].get("width")
	return int(width) if width else None

def is_animated(info: dict) -> bool:
	"""A still image probes as a 1-frame video, so check for actual motion."""
	frames = (info.get("streams") or [{}])[0].get("nb_frames", "")
	if frames.isdigit() and int(frames) > 1:
		return True
	duration = probe_duration(info)
	return duration is not None and duration > 0.1

########## ======================================================================== ##########

def _trim_args(start: float | None, duration: float | None) -> list[str]:
	args = []
	if start:
		args += ["-ss", f"{start:.3f}"]
	if duration:
		args += ["-t", f"{duration:.3f}"]
	return args

def _filters(fps: float | None, width: int | None, reverse: bool, speed: float = 1.0) -> list[str]:
	filters = []
	if speed != 1.0:
		filters.append(f"setpts={1 / speed:.6f}*PTS")
	if fps:
		filters.append(f"fps={fps:.3f}")
	if width:
		filters.append(f"scale='min({width},iw)':-1:flags=lanczos")  # only ever scales down
	if reverse:
		filters.append("reverse")
	return filters

async def extract_frames(src: str, frames_dir: str, fps: float | None, width: int | None, start: float | None, duration: float | None, reverse: bool, speed: float = 1.0) -> list[str]:
	"""Decode into numbered PNGs for gifski."""
	await asyncio.to_thread(os.makedirs, frames_dir, exist_ok=True)

	args = ["ffmpeg", "-y"] + _trim_args(start, duration) + ["-i", src, "-vsync", "0"]
	filters = _filters(fps, width, reverse, speed)
	if filters:
		args += ["-vf", ",".join(filters)]
	args += [os.path.join(frames_dir, "f%05d.png")]

	code, _, _ = await run(*args)
	if code != 0:
		return []
	names = await asyncio.to_thread(os.listdir, frames_dir)
	return sorted(os.path.join(frames_dir, f) for f in names if f.endswith(".png"))

async def still_gif(src: str, dst: str, width: int | None, target_bytes: int) -> bytes | None:
	"""Still images go through ffmpeg's palette instead of gifski"""
	scale = f"scale='min({width},iw)':-1:flags=lanczos," if width else ""
	vf = f"{scale}split[a][b];[a]palettegen=stats_mode=full[p];[b][p]paletteuse"
	code, _, _ = await run("ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", "1", dst)
	if code != 0:
		return None
	return await read_if_fits(dst, target_bytes)

async def gifski_encode(frames, dst: str, fps: float, quality: int, loop_forever: bool, target_bytes: int) -> tuple[bytes | None, int]:
	"""Returns (gif, size). The size drives the next guess. Takes either a list of PNG paths or a single .y4m file."""
	args = ["gifski", "--fps", f"{max(fps, 1):.3f}", "--quality", str(quality)]
	args += ["--repeat", "0" if loop_forever else "-1"]
	args += ["-o", dst] + ([frames] if isinstance(frames, str) else list(frames))
	code, _, _ = await run(*args)
	if code != 0 or not os.path.exists(dst):
		return None, 0
	size = os.path.getsize(dst)
	if size > target_bytes:
		return None, size
	return await asyncio.to_thread(_read_file, dst) or None, size

########## ======================================================================== ##########

async def to_gif(data: bytes, suffix: str, target_bytes: int, *, fps: float | None = None, width: int | None = None, quality: int = 90, start: float | None = None, duration: float | None = None, reverse: bool = False, loop_forever: bool = True, speed: float = 1.0) -> tuple[bytes | None, str | None]:
	"""Convert image/video bytes to a GIF under target_bytes. Returns (gif, error).
	Steps width and quality down from what was asked for until it fits."""
	global _queued
	_queued += 1
	try:
		async with _slots:
			return await _convert(data, suffix, target_bytes, fps, width, quality, start, duration, reverse, loop_forever, speed)
	finally:
		_queued -= 1

async def _convert(data: bytes, suffix: str, target_bytes: int, fps: float | None, width: int | None, quality: int, start: float | None, duration: float | None, reverse: bool, loop_forever: bool, speed: float) -> tuple[bytes | None, str | None]:
	with tempfile.TemporaryDirectory() as tmp:
		src = os.path.join(tmp, f"in{suffix}")
		await write_file(src, data)

		info = await probe(src)
		if not info:
			return None, "Couldn't read that file, it's either corrupt or not an image/video."

		source_duration = probe_duration(info)
		animated = is_animated(info)

		if not animated:
			# fps/trim/reverse/speed mean nothing for a still
			start, duration, reverse, fps, speed = None, None, False, None, 1.0
		else:
			if duration is None and start is not None and source_duration is not None:
				duration = max(source_duration - start, 0)
			effective = duration if duration is not None else source_duration
			if effective is not None:
				if effective <= 0:
					return None, "That start time is past the end of the video."
				# the cap is on output length, and slowing down stretches it
				if effective / speed > MAX_GIF_SECONDS:
					duration = MAX_GIF_SECONDS * speed
			fps = fps or min(probe_fps(info), 30)

		source_width = probe_width(info)
		requested_width = width or (min(source_width, 800) if source_width else 800)

		if not animated:
			for step_width in (requested_width, 640, 480, 360, 280):
				if step_width > requested_width:
					continue
				out = await still_gif(src, os.path.join(tmp, f"still_{step_width}.gif"), step_width, target_bytes)
				if out:
					return out, None
			return None, "Couldn't get the GIF small enough to upload. Try a lower width."

		decoded = False
		step_width, step_fps, step_quality = requested_width, fps, quality
		for attempt in range(4):
			frames_dir = os.path.join(tmp, f"fr{attempt}")
			frames = await extract_frames(src, frames_dir, step_fps, step_width, start, duration, reverse, speed)
			if not frames:
				if attempt == 0 and (duration or speed != 1.0):
					# the trim/speed combination didn't leave a single whole frame
					return None, "That's too short to make a GIF out of. Try a longer clip or a lower speed."
				break
			decoded = True

			if len(frames) == 1:
				one_width = step_width
				for _ in range(5):
					out = await still_gif(frames[0], os.path.join(tmp, f"one{one_width}.gif"), one_width, target_bytes)
					if out:
						return out, None
					if one_width <= 160:
						break
					one_width = max(int(one_width * 0.7), 160)
				break

			out, size = await gifski_encode(frames, os.path.join(tmp, f"out{attempt}.gif"), step_fps, step_quality, loop_forever, target_bytes)
			if out:
				return out, None
			await asyncio.to_thread(shutil.rmtree, frames_dir, True)
			if not size:
				break

			overshoot = size / target_bytes
			next_width = max(int(step_width / overshoot ** 0.5 * 0.88), 240)
			next_quality = max(step_quality - 15, 45)
			next_fps = max(step_fps * 0.7, 12) if (overshoot > 2 and step_fps) else step_fps
			# only give up once nothing can shrink anymore
			if next_width >= step_width and next_quality >= step_quality and next_fps >= (step_fps or 0):
				break
			step_width, step_quality, step_fps = next_width, next_quality, next_fps

	if not decoded:
		return None, "Couldn't decode that file, it may be corrupt or in a format ffmpeg doesn't support."
	return None, "Couldn't get the GIF small enough to upload. Try a shorter clip, a lower width, or a lower quality."
