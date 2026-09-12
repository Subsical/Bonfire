import asyncio
import functools
import io
import math
import os
import random
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from lib import media, theme

MAX_OPTIONS = 20
MAX_OPTION_LENGTH = 20

WHEEL = {
	"size": 400,					# output size in pixels
	"supersample": 2,				# how much larger the face is drawn, for smooth edges
	"rotate_scale": 1.4,			# size multiple frames are turned at
	"fps": 24,

	"spin_seconds": 5.5,			# length of the spin
	"pause_seconds": 0.6,			# rest on the winner
	"fade_seconds": 0.45,			# fade in the dark gradient
	"hold_seconds": 2.0,			# hold the result

	"turns": 10,					# full rotations per spin
	"min_turns": 5,					# fewest, even when the limit below says slower
	"max_wedges_per_frame": 1.1,	# fastest the wheel can sweep past, before it smears
	"friction": 0.65,				# lower keeps its speed for longer
	"tail_power": 3.0,				# how sharply it settles

	"min_font_size": 15,			# in supersampled pixels
	"max_lines": 3,					# per label, before the font shrinks
	"line_spacing": 1.15,			# multiple of font size

	"pointer_angle": 270,			# clockwise from 12 o'clock
	"dim_centre": 0.82,				# result gradient, behind the text
	"dim_edge": 0.45,				# result gradient, at the rim
}

BACKGROUND = "#1E1F22"
POINTER_COLOR = "#1E1F22"
TEXT_LIGHT = "#FFFFFF"
TEXT_DARK = "#1A1A1A"
PALETTE = [
	theme.MAIN, theme.ALT, "#6CBE78", "#56AADC",
	"#968CE1", "#E178AA", "#EB6E64", "#78C8C3",
]

########## ======================================================================== ##########

def parse_options(raw: str) -> list[str]:
	"""Split the user's comma separated list properly."""
	options = [part.strip() for part in raw.split(",")]
	return [o for o in options if o]

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
FONT_PATHS = [
	os.path.join(ASSETS, "Inter-Bold.ttf"),
	os.path.join(ASSETS, "NotoSansKR-Bold.otf"),
	os.path.join(ASSETS, "NotoSansSC-Bold.otf"),
]

@functools.lru_cache(maxsize=256)
def _font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont:
	path = path or FONT_PATHS[0]
	if os.path.exists(path):
		return ImageFont.truetype(path, size)
	return ImageFont.load_default(size)

@functools.lru_cache(maxsize=4096)
def _covers(path: str, char: str) -> bool:
	"""Whether a font has a real glyph for this character, rather than an empty box."""
	if not os.path.exists(path):
		return False
	font = _font(40, path)
	glyph, missing = font.getmask(char), font.getmask("\uffff")
	return bytes(glyph) != bytes(missing) if glyph.size == missing.size else True

def _runs(text: str, size: int) -> list[tuple[ImageFont.FreeTypeFont, str]]:
	"""Split text into chunks, each with the first font that can actually draw it."""
	chunks: list[list] = []
	for char in text:
		path = next((p for p in FONT_PATHS if _covers(p, char)), FONT_PATHS[0])
		if chunks and chunks[-1][0] == path:
			chunks[-1][1].append(char)
		else:
			chunks.append([path, [char]])
	return [(_font(size, path), "".join(cs)) for path, cs in chunks]

def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
	return sum(draw.textlength(chunk, font=f) for f, chunk in _runs(text, font.size))

def _draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill):
	x, y = xy
	for chunk_font, chunk in _runs(text, font.size):
		draw.text((x, y), chunk, font=chunk_font, fill=fill)
		x += draw.textlength(chunk, font=chunk_font)

def _wrap(label: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw, max_width: float) -> list[str]:
	"""Breaks a label into lines that fit the width. Never splits a word."""
	words = label.split()
	if not words:
		return [label]
	lines, current = [], words[0]
	for word in words[1:]:
		candidate = f"{current} {word}"
		if _measure(draw, candidate, font) <= max_width:
			current = candidate
		else:
			lines.append(current)
			current = word
	lines.append(current)
	return lines

def _block_size(lines: list[str], font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> tuple[float, float]:
	"""Width and height of a wrapped label."""
	width = max(_measure(draw, line, font) for line in lines)
	return width, len(lines) * font.size * WHEEL["line_spacing"]

def _plan_labels(options: list[str], draw: ImageDraw.ImageDraw, radius: float, hub: float, wedge: float, start_size: int) -> tuple[ImageFont.FreeTypeFont, dict[str, list[str]]]:
	"""One font size and wrapping for the whole wheel, sized to whichever label needs most room."""
	available = radius - hub - radius * 0.10

	size = start_size
	while size > WHEEL["min_font_size"]:
		font = _font(size)
		wrapped = {option: _wrap(option, font, draw, available) for option in options}
		widest = max(_block_size(lines, font, draw)[0] for lines in wrapped.values())
		tallest = max(len(lines) for lines in wrapped.values()) * font.size * WHEEL["line_spacing"]
		across = 2 * (hub + available * 0.55) * math.tan(math.radians(min(wedge, 180) / 2))
		if widest <= available and tallest <= across and max(len(l) for l in wrapped.values()) <= WHEEL["max_lines"]:
			return font, wrapped
		size -= 1

	font = _font(WHEEL["min_font_size"])
	return font, {o: _wrap(o, font, draw, available) for o in options}

########## ======================================================================== ##########

def _wedge_colors(count: int) -> list[str]:
	"""Cycles the palette round, nudging the last wedge if it wraps onto the first's colour."""
	colors = [PALETTE[i % len(PALETTE)] for i in range(count)]
	if count > 2 and colors[-1] == colors[0]:
		colors[-1] = PALETTE[(count - 2) % len(PALETTE) - 1]
	return colors

def _wheel_face(options: list[str], font: ImageFont.FreeTypeFont, wrapped: dict[str, list[str]],
		size: int, radius: float, hub: float) -> Image.Image:
	"""The wheel's face at rest, drawn once and then rotated for each frame."""
	scale = WHEEL["supersample"]
	image = Image.new("RGB", (size, size), BACKGROUND)
	draw = ImageDraw.Draw(image)

	centre = size / 2
	margin = centre - radius
	box = (margin, margin, size - margin, size - margin)
	wedge = 360 / len(options)
	colors = _wedge_colors(len(options))
	line_height = font.size * WHEEL["line_spacing"]

	for i, option in enumerate(options):
		start = -90 + i * wedge
		fill = colors[i]
		draw.pieslice(box, start, start + wedge, fill=fill, outline=BACKGROUND, width=max(scale, 2))

		lines = wrapped[option]
		block_width = max(_measure(draw, line, font) for line in lines)
		block_height = len(lines) * line_height

		pad = 4 * scale
		text_image = Image.new("RGBA", (int(block_width + pad * 2), int(block_height + pad * 2)), (0, 0, 0, 0))
		text_draw = ImageDraw.Draw(text_image)
		for row, line in enumerate(lines):
			width = _measure(draw, line, font)
			_draw_text(text_draw, (pad + (block_width - width) / 2, pad + row * line_height), line, font, TEXT_DARK)

		# the pointer's label always reads the right way up
		angle = start + wedge / 2
		text_image = text_image.rotate(-(angle + 180), expand=True, resample=Image.BICUBIC)

		label_radius = hub + (radius - hub) / 2
		mid = math.radians(angle)
		x = centre + math.cos(mid) * label_radius - text_image.width / 2
		y = centre + math.sin(mid) * label_radius - text_image.height / 2
		image.paste(text_image, (int(x), int(y)), text_image)

	return image

def _draw_overlay(image: Image.Image, radius: float):
	"""The hub and pointer, which stay put while the wheel turns underneath."""
	scale = WHEEL["supersample"]
	draw = ImageDraw.Draw(image)
	centre = image.width / 2
	margin = centre - radius

	hub = radius * 0.11
	draw.ellipse((centre - hub, centre - hub, centre + hub, centre + hub), fill=BACKGROUND, outline=POINTER_COLOR, width=max(2 * scale, 2))

	tip_x = margin + radius * 0.14
	back_x = margin - 15 * scale
	half = radius * 0.08
	draw.polygon([(tip_x, centre), (back_x, centre - half), (back_x, centre + half)],
		fill=POINTER_COLOR, outline=BACKGROUND, width=max(scale, 2))

class Wheel:
	"""A wheel whose face is drawn once, then just turned for each frame"""

	def __init__(self, options: list[str]):
		self.options = options
		scale = WHEEL["supersample"]
		self.size = WHEEL["size"] * scale
		self.radius = self.size / 2 - 22 * scale
		self.hub = self.radius * 0.11
		self.wedge = 360 / len(options)

		probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
		start_size = max(int(self.radius * 0.115), WHEEL["min_font_size"])
		self.font, self.wrapped = _plan_labels(options, probe, self.radius, self.hub, self.wedge, start_size)
		face = _wheel_face(options, self.font, self.wrapped, self.size, self.radius, self.hub)

		self.spin_size = int(WHEEL["size"] * WHEEL["rotate_scale"])
		self.face = face.resize((self.spin_size, self.spin_size), Image.LANCZOS)
		self.out_radius = self.radius * WHEEL["size"] / self.size

	def frame(self, rotation: float) -> Image.Image:
		"""One frame, turned `rotation` degrees clockwise."""
		# pillow maps output pixels back through this, so the matrix is the inverse
		angle = math.radians(rotation)
		scale = self.spin_size / WHEEL["size"]
		cos, sin = math.cos(angle) * scale, math.sin(angle) * scale
		centre, half = self.spin_size / 2, WHEEL["size"] / 2
		matrix = (
			cos, sin, centre - half * cos - half * sin,
			-sin, cos, centre + half * sin - half * cos,
		)
		image = self.face.transform((WHEEL["size"], WHEEL["size"]), Image.AFFINE, matrix,
			resample=Image.BICUBIC, fillcolor=BACKGROUND)
		_draw_overlay(image, self.out_radius)
		return image

def _dim_mask(size: int) -> Image.Image:
	"""A round gradient, heaviest in the middle. Built small and stretched up, it's smooth either way."""
	steps = 96
	small = Image.new("L", (steps, steps))
	pixels = small.load()
	for y in range(steps):
		for x in range(steps):
			dx = (x - steps / 2) / (steps / 2)
			dy = (y - steps / 2) / (steps / 2)
			t = min(math.hypot(dx, dy), 1.0)
			strength = WHEEL["dim_edge"] + (WHEEL["dim_centre"] - WHEEL["dim_edge"]) * (1 - t) ** 1.5
			pixels[x, y] = int(255 * strength)
	return small.resize((size, size), Image.BICUBIC)

def _result_image(frame: Image.Image, winner: str) -> Image.Image:
	"""The wheel's last frame behind a dark gradient, with the winner across the middle."""
	scale = WHEEL["supersample"]
	size = frame.width * scale
	base = frame.resize((size, size), Image.LANCZOS).convert("RGB")

	gradient = Image.new("RGB", (size, size), (0, 0, 0))
	image = Image.composite(gradient, base, _dim_mask(size))

	draw = ImageDraw.Draw(image)
	radius = size / 2 - 22 * scale
	available = radius * 1.7

	size_guess = int(radius * 0.30)
	while size_guess > WHEEL["min_font_size"]:
		font = _font(size_guess)
		lines = _wrap(winner, font, draw, available)
		widest = max(_measure(draw, line, font) for line in lines)
		if len(lines) <= WHEEL["max_lines"] and widest <= available:
			break
		size_guess -= 2
	else:
		font, lines = _font(WHEEL["min_font_size"]), _wrap(winner, _font(WHEEL["min_font_size"]), draw, available)

	line_height = font.size * WHEEL["line_spacing"]
	top = size / 2 - len(lines) * line_height / 2
	for row, line in enumerate(lines):
		width = _measure(draw, line, font)
		_draw_text(draw, (size / 2 - width / 2 + scale * 2, top + row * line_height + scale * 2), line, font, "#000000")
		_draw_text(draw, (size / 2 - width / 2, top + row * line_height), line, font, TEXT_LIGHT)

	return image.resize((frame.width, frame.width), Image.LANCZOS)

def _draw_wheel(options: list[str], rotation: float) -> Image.Image:
	"""A single frame, for callers that only want one."""
	return Wheel(options).frame(rotation)

########## ======================================================================== ##########

def _rotation_for(index: int, count: int) -> float:
	"""Rotation that brings the winning wedge to the pointer, landing anywhere inside it."""
	wedge = 360 / count
	jitter = random.uniform(-wedge * 0.44, wedge * 0.44)
	return _turns(count) * 360 + WHEEL["pointer_angle"] - (index + 0.5) * wedge + jitter

def _turns(count: int) -> float:
	"""How many full rotations to spin. Thin wedges turn fewer times so they don't blur past."""
	wedge = 360 / count
	peak = _peak_fraction() * 360  # degrees in the fastest frame, per full turn of travel
	allowed = WHEEL["max_wedges_per_frame"] * wedge / peak
	# whole turns only, so the spin-up doesn't shift where the wheel comes to rest
	return float(max(min(WHEEL["turns"], int(allowed)), WHEEL["min_turns"]))

def _peak_fraction() -> float:
	"""The largest share of the spin any one frame covers."""
	frames = max(int(WHEEL["spin_seconds"] * WHEEL["fps"]), 1)
	return max(_ease_out((f + 1) / frames) - _ease_out(f / frames) for f in range(frames))

def _ease_out(t: float) -> float:
	"""How far through its travel the wheel is, `t` of the way through the spin.
	A cubic ease-out blended with a slower tail, so it stays fast for longer then creeps to a stop."""
	cubic = t * t * t - 3 * t * t + 3 * t
	return WHEEL["friction"] * cubic + (1 - WHEEL["friction"]) * (1 - (1 - t) ** WHEEL["tail_power"])

########## ======================================================================== ##########

def total_seconds() -> float:
	"""When the result is fully on screen, so the caller knows when to swap in the still."""
	return WHEEL["spin_seconds"] + WHEEL["pause_seconds"] + WHEEL["fade_seconds"]

async def spin(options: list[str], target_bytes: int) -> tuple[bytes | None, bytes | None, int, str | None]:
	"""Picks a winner and renders the spin. Returns (gif, result_png, winner_index, error)."""
	winner = random.randrange(len(options))
	async with media.slot():
		gif, still, error = await _build(options, winner, target_bytes)
	return gif, still, winner, error

async def _build(options: list[str], winner: int, target_bytes: int) -> tuple[bytes | None, bytes | None, str | None]:
	"""Draws the frames and hands them to gifski. The final rotation is worked out once, so the
	GIF's last frame and the still are the same image."""
	final_rotation = _rotation_for(winner, len(options))

	with tempfile.TemporaryDirectory() as tmp:
		source = os.path.join(tmp, "wheel.y4m")
		result = await asyncio.to_thread(_render, options, final_rotation, source, options[winner])
		if result is None:
			return None, None, "Something went wrong while drawing the wheel."

		still = await asyncio.to_thread(_encode_png, result)
		dst = os.path.join(tmp, "wheel.gif")
		# more wedges means more colour per frame, so denser wheels start lower
		quality = 90 if len(options) <= 8 else (70 if len(options) <= 14 else 55)
		for _ in range(3):
			gif, size = await media.gifski_encode(source, dst, WHEEL["fps"], quality, False, target_bytes)
			if gif:
				return gif, still, None
			if not size or quality <= 40:
				break
			quality = max(quality - 20, 40)

		return None, None, "I couldn't get the wheel small enough to upload."

def _write_y4m(out, frames, fps: int):
	"""Writes frames to a stream as raw YUV video, which gifski reads directly.
	Saves encoding a few hundred PNGs, which costs more than the GIF itself."""
	header = False
	for image in frames:
		if not header:
			out.write(f"YUV4MPEG2 W{image.width} H{image.height} F{fps}:1 Ip A1:1 C444 XCOLORRANGE=FULL\n".encode())
			header = True
		rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
		r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
		# the RGB->YUV matrix gifski expects at this size, using the full 0-255 range
		y = 0.299*r + 0.587*g + 0.114*b
		u = -0.168736*r - 0.331264*g + 0.5*b + 128
		v = 0.5*r - 0.418688*g - 0.081312*b + 128
		out.write(b"FRAME\n")
		for plane in (y, u, v):
			out.write(np.clip(plane, 0, 255).astype(np.uint8).tobytes())

def _render(options: list[str], final_rotation: float, path: str, winner: str) -> Image.Image:
	"""Renders the spin into a y4m file for gifski, and returns the result still.
	gifski has to be able to seek its input, so this can't just be piped in."""
	result = None

	def tracked():
		nonlocal result
		for image in _frames(options, final_rotation, winner):
			result = image
			yield image

	with open(path, "wb") as out:
		_write_y4m(out, tracked(), WHEEL["fps"])
	return result

def _frames(options: list[str], final_rotation: float, winner: str):
	"""Yields every frame of the spin, then the result it settles on."""
	spin_frames = max(int(WHEEL["spin_seconds"] * WHEEL["fps"]), 1)
	pause_frames = max(int(WHEEL["pause_seconds"] * WHEEL["fps"]), 1)
	fade_frames = max(int(WHEEL["fade_seconds"] * WHEEL["fps"]), 1)
	hold_frames = max(int(WHEEL["hold_seconds"] * WHEEL["fps"]), 1)

	spinner = Wheel(options)
	for frame in range(spin_frames):
		yield spinner.frame(final_rotation * _ease_out((frame + 1) / spin_frames))

	resting = spinner.frame(final_rotation)
	for _ in range(pause_frames):
		yield resting

	result = _result_image(resting, winner)
	for frame in range(fade_frames):
		yield Image.blend(resting, result, (frame + 1) / fade_frames)
	for _ in range(hold_frames):
		yield result

def _encode_png(image: Image.Image) -> bytes:
	buffer = io.BytesIO()
	image.save(buffer, format="PNG")
	return buffer.getvalue()
