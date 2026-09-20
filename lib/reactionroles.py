import json
import re
import traceback

import discord

from lib import database as db
from lib import theme

MAX_PANELS = 25
MAX_OPTIONS = 25
MAX_TITLE = 100
MAX_CONTENT = 2000
MAX_LABEL = 80
MAX_DESCRIPTION = 100
# five to a row overflows once labels carry an emoji, so four is the default
PER_ROW = 4
MAX_PER_ROW = 5
ROWS = 5
MAX_BUTTONS = MAX_PER_ROW * ROWS

STYLES = {"reaction", "button", "select"}
MODES = {"multiple", "single", "limited"}

# <:name:id> and <a:name:id>, how a custom emoji is stored and sent back
CUSTOM_EMOJI = re.compile(r"<(a?):(\w{2,32}):(\d+)>")

####### =================================================================== #######

def panel_dict(row) -> dict:
	panel_id, guild_id, channel_id, message_id, owned, title, content, embed, style, mode, limit_count, per_row = row
	return {
		"id": panel_id, "guild_id": guild_id, "channel_id": channel_id,
		"message_id": message_id, "owned": bool(owned), "title": title,
		"content": content, "embed": json.loads(embed) if embed else None,
		"style": style, "mode": mode, "limit": limit_count,
		"per_row": per_row or PER_ROW,
	}

def get_panel(panel_id: int, guild_id: int | None = None) -> dict | None:
	row = db.get_panel(panel_id, guild_id)
	return panel_dict(row) if row else None

def options_of(panel_id: int) -> list[dict]:
	return [
		{"role_id": role_id, "emoji": emoji, "label": label, "description": description}
		for role_id, emoji, label, description, _ in db.panel_options(panel_id)
	]

def assignable(guild: discord.Guild, role_id: int) -> discord.Role | None:
	"""The role, if the bot is actually allowed to hand it out."""
	if guild.me is None or not guild.me.guild_permissions.manage_roles:
		return None
	role = guild.get_role(role_id)
	if role is None or role.managed or role >= guild.me.top_role:
		return None
	return role

def partial_emoji(raw: str | None) -> discord.PartialEmoji | None:
	"""A stored emoji as something Discord will take back."""
	if not raw:
		return None
	match = CUSTOM_EMOJI.fullmatch(raw.strip())
	if match:
		animated, name, emoji_id = match.groups()
		return discord.PartialEmoji(name=name, id=int(emoji_id), animated=bool(animated))
	return discord.PartialEmoji(name=raw.strip())

def usable_emoji(raw: str) -> bool:
	"""Whether Discord will take this as a reaction."""
	raw = raw.strip()
	if CUSTOM_EMOJI.fullmatch(raw):
		return True
	return bool(raw) and len(raw) <= 8 and not raw.isascii()

def emoji_key(emoji) -> str:
	"""How an emoji is matched between a stored option and a live reaction."""
	if isinstance(emoji, str):
		emoji = partial_emoji(emoji)
	if emoji is None:
		return ""
	return str(emoji.id) if emoji.id else emoji.name

####### =================================================================== #######

def _held(member: discord.Member, options: list[dict]) -> list[int]:
	"""Which of a panel's roles the member already has."""
	on_panel = {option["role_id"] for option in options}
	return [role.id for role in member.roles if role.id in on_panel]

async def apply_pick(member: discord.Member, panel: dict, role_id: int, adding: bool, options: list[dict] | None = None) -> tuple[str, bool, list[int]]:
	"""Gives or takes one of a panel's roles, obeying its mode."""
	role = assignable(member.guild, role_id)
	if role is None:
		return f"{theme.ERR} I don't have sufficient permissions to give that role.", False, []

	options = options if options is not None else options_of(panel["id"])
	has = role in member.roles
	reason = f"Reaction roles panel #{panel['id']}"

	if not adding or has:
		if not has:
			return f"{theme.ERR} You don't have that role.", True, []
		await member.remove_roles(role, reason=reason)
		return f"Took away {role.mention}.", True, []

	held = _held(member, options)
	mode, limit = panel["mode"], panel["limit"]

	if mode == "single" and held:
		drop = [member.guild.get_role(other) for other in held if other != role.id]
		drop = [item for item in drop if item is not None]
		if drop:
			# one edit rather than an add and a remove, so the swap isn't two round trips
			dropping = {item.id for item in drop}
			# member.roles carries @everyone, which Discord won't accept back
			keep = [item for item in member.roles if item.id not in dropping and not item.is_default()]
			await member.edit(roles=[*keep, role], reason=reason)
			return f"{theme.SUC} Swapped you to {role.mention}.", True, list(dropping)
		await member.add_roles(role, reason=reason)
		return f"{theme.SUC} Gave you {role.mention}.", True, []

	if mode == "limited" and limit and len(held) >= limit:
		return f"{theme.ERR} You can only have {limit} of these roles. Remove one first.", False, []

	await member.add_roles(role, reason=reason)
	return f"{theme.SUC} Gave you {role.mention}.", True, []

####### =================================================================== #######

class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bonfire_role:(?P<panel_id>\d+):(?P<role_id>\d+)"):
	def __init__(self, panel_id: int, role_id: int, label: str = "​", emoji=None, row: int | None = None):
		super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.gray, custom_id=f"bonfire_role:{panel_id}:{role_id}", row=row))
		self.panel_id = panel_id
		self.role_id = role_id

	@classmethod
	async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
		return cls(int(match["panel_id"]), int(match["role_id"]))

	async def callback(self, interaction: discord.Interaction):
		panel = get_panel(self.panel_id)
		if panel is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("This panel no longer exists.", ephemeral=True)
			return
		if not db.module_enabled(panel["guild_id"], "reaction-roles"):
			await interaction.response.send_message("Reaction roles are turned off in this server.", ephemeral=True)
			return
		told, _, _ = await apply_pick(interaction.user, panel, self.role_id, adding=True)
		await interaction.response.send_message(told or "Nothing changed.", ephemeral=True)

class RoleSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"bonfire_rolemenu:(?P<panel_id>\d+)"):
	def __init__(self, panel_id: int, options: list[dict] | None = None, mode: str = "multiple"):
		options = options if options is not None else options_of(panel_id)
		choices = [
			discord.SelectOption(
				label=(option["label"] or str(option["role_id"]))[:MAX_LABEL],
				value=str(option["role_id"]),
				description=option["description"][:MAX_DESCRIPTION] or None,
				emoji=partial_emoji(option["emoji"]),
			)
			for option in options
		] or [discord.SelectOption(label="No roles yet", value="none")]
		top = 1 if mode == "single" else len(choices)
		super().__init__(discord.ui.Select(
			custom_id=f"bonfire_rolemenu:{panel_id}",
			placeholder="Pick a role" if mode == "single" else "Pick roles to add or remove",
			options=choices, min_values=0, max_values=max(1, min(top, len(choices))),
		))
		self.panel_id = panel_id

	@classmethod
	async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
		return cls(int(match["panel_id"]))

	async def callback(self, interaction: discord.Interaction):
		panel = get_panel(self.panel_id)
		if panel is None or not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("This panel no longer exists.", ephemeral=True)
			return
		if not db.module_enabled(panel["guild_id"], "reaction-roles"):
			await interaction.response.send_message("Reaction roles are turned off in this server.", ephemeral=True)
			return

		picked = [int(value) for value in self.item.values if value != "none"]
		await interaction.response.defer(ephemeral=True)

		# one read for the whole selection rather than one per role
		options = options_of(self.panel_id)
		lines = []
		for role_id in picked:
			has = any(role.id == role_id for role in interaction.user.roles)
			told, _, _ = await apply_pick(interaction.user, panel, role_id, adding=not has, options=options)
			if told and told not in lines:
				lines.append(told)
		await interaction.followup.send("\n".join(lines) or "Nothing changed.", ephemeral=True)

####### =================================================================== #######

class PanelView(discord.ui.View):
	"""The buttons or dropdown under a panel's message."""

	def __init__(self, panel: dict, options: list[dict] | None = None):
		super().__init__(timeout=None)
		options = options if options is not None else options_of(panel["id"])

		if panel["style"] == "select":
			self.add_item(RoleSelect(panel["id"], options, panel["mode"]))
			return

		per_row = panel.get("per_row") or PER_ROW
		for index, option in enumerate(options[:per_row * ROWS]):
			self.add_item(RoleButton(
				panel["id"], option["role_id"],
				(option["label"] or "​")[:MAX_LABEL],
				partial_emoji(option["emoji"]),
				row=index // per_row,
			))

	async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
		traceback.print_exception(type(error), error, error.__traceback__)
		if not interaction.response.is_done():
			await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)
		else:
			await interaction.followup.send("Something went wrong, try again.", ephemeral=True)

def mode_line(panel: dict) -> str:
	if panel["mode"] == "single":
		return "Pick one role."
	if panel["mode"] == "limited" and panel["limit"]:
		return f"Pick up to {panel['limit']} roles."
	return "Pick as many roles as you like."

####### =================================================================== #######

async def get_channel(bot: discord.Client, channel_id: int, guild_id: int | None = None):
	"""Try to get a channel from the cache or fetch it from the API."""
	channel = bot.get_channel(channel_id)
	if channel is not None:
		return channel
	if guild_id is not None and bot.get_guild(guild_id) is None:
		return None
	try:
		return await bot.fetch_channel(channel_id)
	except (discord.NotFound, discord.Forbidden):
		return None

def build_embed(spec) -> discord.Embed | None:
	"""Turns a panel's saved embed into a real one."""
	if not isinstance(spec, dict) or not spec:
		return None

	def text(key, limit):
		return str(spec.get(key) or "")[:limit] or None

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
		name = str(field.get("name") or "")[:256]
		body = str(field.get("value") or "")[:1024]
		if name and body:
			embed.add_field(name=name, value=body, inline=bool(field.get("inline")))
	return embed

async def post_panel(bot: discord.Client, panel: dict):
	"""Sends a panel's message, or edits it if one is already up."""
	channel = await get_channel(bot, panel["channel_id"], panel["guild_id"])
	if channel is None:
		return None
	options = options_of(panel["id"])

	embed = build_embed(panel["embed"])
	# Discord won't take a message with nothing in it, so the name stands in
	body = panel["content"] or (None if embed else f"## {panel['title']}")
	reacting = panel["style"] == "reaction"
	message = await _send_or_edit(
		channel, panel,
		content=body,
		embeds=[embed] if embed else [],
		view=None if reacting else PanelView(panel, options),
	)
	if message is not None:
		# a panel that used to be reactions would leave its emoji sitting there
		await sync_reactions(message, options if reacting else [])
	return message

async def _send_or_edit(channel, panel: dict, **kwargs):
	if panel["message_id"]:
		try:
			message = await channel.fetch_message(panel["message_id"])
			await message.edit(**kwargs)
			return message
		except (discord.NotFound, discord.Forbidden):
			return None
		except discord.HTTPException as error:
			if not _is_v2_clash(error) or not panel["owned"]:
				raise
			try:
				await message.delete()
			except (discord.NotFound, discord.Forbidden, discord.HTTPException):
				pass
	kwargs = {key: value for key, value in kwargs.items() if value}
	try:
		message = await channel.send(**kwargs)
	except discord.Forbidden:
		return None
	db.set_panel_message(panel["id"], message.id)
	return message

def _is_v2_clash(error: discord.HTTPException) -> bool:
	"""Whether Discord refused an edit because the message is a Components V2 one."""
	return error.status == 400 and "IS_COMPONENTS_V2" in str(error)

async def sync_reactions(message: discord.Message, options: list[dict]):
	"""Puts the panel's emoji on the message, and clears off any that no longer belong."""
	wanted = {emoji_key(option["emoji"]) for option in options if option["emoji"]}
	already = set()
	for reaction in message.reactions:
		key = emoji_key(reaction.emoji)
		if reaction.me and key not in wanted:
			try:
				await message.clear_reaction(reaction.emoji)
			except (discord.Forbidden, discord.NotFound, discord.HTTPException):
				pass
		elif reaction.me:
			already.add(key)
	for option in options:
		emoji = partial_emoji(option["emoji"])
		if emoji is None or emoji_key(emoji) in already:
			continue
		try:
			await message.add_reaction(emoji)
		except (discord.Forbidden, discord.NotFound, discord.HTTPException):
			pass

async def refresh_panel(bot: discord.Client, panel_id: int):
	"""Re-renders a panel's live message. Fails silently if it's gone."""
	panel = get_panel(panel_id)
	if panel is None or panel["message_id"] is None:
		return
	if not panel["owned"]:
		channel = await get_channel(bot, panel["channel_id"], panel["guild_id"])
		if channel is None:
			return
		try:
			message = await channel.fetch_message(panel["message_id"])
		except (discord.NotFound, discord.Forbidden):
			return
		await sync_reactions(message, options_of(panel_id))
		return
	await post_panel(bot, panel)

async def reclaim_panels(bot: discord.Client):
	"""Marks panels as Bonfire's own where it sent the message itself."""
	fixed = 0
	for panel_id, guild_id, channel_id, message_id in db.unowned_panels():
		channel = await get_channel(bot, channel_id, guild_id)
		if channel is None:
			continue
		try:
			message = await channel.fetch_message(message_id)
		except (discord.NotFound, discord.Forbidden, discord.HTTPException):
			continue
		if message.author.id == bot.user.id:
			db.set_panel_owned(panel_id, True)
			fixed += 1
	if fixed:
		print(f"Reclaimed {fixed} role panel{'' if fixed == 1 else 's'} Bonfire had posted itself.")

async def delete_panel(bot: discord.Client, panel_id: int, guild_id: int):
	"""Removes a panel, taking its message with it when Bonfire posted it."""
	panel = get_panel(panel_id, guild_id)
	if panel is None:
		return
	db.delete_panel(panel_id, guild_id)
	if panel["message_id"] is None:
		return
	channel = await get_channel(bot, panel["channel_id"], panel["guild_id"])
	if channel is None:
		return
	try:
		message = await channel.fetch_message(panel["message_id"])
	except (discord.NotFound, discord.Forbidden):
		return
	try:
		if panel["owned"]:
			await message.delete()
		else:
			await message.clear_reactions()
	except (discord.Forbidden, discord.NotFound, discord.HTTPException):
		pass

####### =================================================================== #######

async def on_reaction(bot: discord.Client, payload: discord.RawReactionActionEvent, adding: bool):
	"""A reaction on a panel message, added or removed."""
	if payload.guild_id is None or (payload.user_id == bot.user.id):
		return
	row = db.panel_by_message(payload.message_id)
	if row is None:
		return
	panel = panel_dict(row)
	if panel["style"] != "reaction" or not db.module_enabled(panel["guild_id"], "reaction-roles"):
		return

	key = emoji_key(payload.emoji)
	options = options_of(panel["id"])
	match = next((option for option in options if emoji_key(option["emoji"]) == key), None)
	if match is None:
		return

	guild = bot.get_guild(payload.guild_id)
	if guild is None:
		return
	member = payload.member if adding else guild.get_member(payload.user_id)
	if member is None or member.bot:
		return

	try:
		_, stuck, dropped = await apply_pick(member, panel, match["role_id"], adding=adding, options=options)
	except discord.Forbidden:
		return
	if not adding:
		return

	stale = [] if stuck else [payload.emoji]
	stale += [
		partial_emoji(option["emoji"])
		for option in options
		if option["role_id"] in dropped and option["emoji"]
	]
	if not stale:
		return

	channel = await get_channel(bot, payload.channel_id, payload.guild_id)
	if channel is None:
		return
	message = channel.get_partial_message(payload.message_id)
	for emoji in stale:
		try:
			await message.remove_reaction(emoji, member)
		except (discord.Forbidden, discord.NotFound, discord.HTTPException):
			pass

####### =================================================================== #######

def _clean_option(raw, style: str) -> dict:
	if not isinstance(raw, dict):
		raise ValueError("A role on the panel isn't valid.")  # noqa: TRY004
	try:
		role_id = int(raw.get("role_id"))
	except (TypeError, ValueError) as error:
		raise ValueError("Pick a real role.") from error

	emoji = str(raw.get("emoji") or "").strip()[:64]
	if emoji and not usable_emoji(emoji):
		raise ValueError(f"{emoji} isn't an emoji I can use.")
	label = str(raw.get("label") or "").strip()[:MAX_LABEL]
	if style == "reaction" and not emoji:
		raise ValueError("Every role needs an emoji on a reaction panel.")
	if style == "button" and not (emoji or label):
		raise ValueError("Every role needs a label or an emoji on a button panel.")
	if style == "select" and not label:
		raise ValueError("Every role needs a label on a dropdown panel.")
	return {
		"role_id": role_id, "emoji": emoji or None, "label": label,
		"description": str(raw.get("description") or "").strip()[:MAX_DESCRIPTION],
	}

def message_id_from(raw: str, guild_id: int) -> int:
	"""A message id out of a jump link or a bare id."""
	raw = str(raw or "").strip()
	link = re.search(r"/channels/(\d+)/(\d+)/(\d+)", raw)
	if link:
		if int(link.group(1)) != guild_id:
			raise ValueError("That message is in another server.")
		return int(link.group(3))
	if not raw.isdigit():
		raise ValueError("That doesn't look like a message link or ID.")
	return int(raw)

def validate(body: dict, owned: bool = True) -> dict:
	"""Checks a panel from the website, raising ValueError with something to show the user."""
	title = str(body.get("title") or "").strip()
	if not title:
		raise ValueError("Give the panel a title.")

	style = body.get("style")
	if style not in STYLES:
		raise ValueError("That isn't a panel style we know.")
	mode = body.get("mode")
	if mode not in MODES:
		raise ValueError("That isn't a pick mode we know.")

	given = body.get("options") or []
	if len(given) > MAX_OPTIONS:
		raise ValueError(f"A panel can have at most {MAX_OPTIONS} roles.")

	options, seen = [], set()
	for raw in given:
		option = _clean_option(raw, style)
		if option["role_id"] in seen:
			raise ValueError("The same role is on the panel twice.")
		seen.add(option["role_id"])
		options.append(option)
	if not options:
		raise ValueError("Put at least one role on the panel.")
	if style == "reaction":
		keys = [emoji_key(option["emoji"]) for option in options]
		if len(set(keys)) != len(keys):
			raise ValueError("Two roles are using the same emoji.")
	try:
		per_row = int(body.get("per_row") or PER_ROW)
	except (TypeError, ValueError) as error:
		raise ValueError("The buttons per row has to be a number.") from error
	if not 1 <= per_row <= MAX_PER_ROW:
		raise ValueError(f"Buttons per row has to be between 1 and {MAX_PER_ROW}.")
	if style == "button" and len(options) > per_row * ROWS:
		raise ValueError(
			f"At {per_row} button{'' if per_row == 1 else 's'} a row that panel fits "
			f"{per_row * ROWS} roles. Widen the rows, or use a dropdown.",
		)

	try:
		limit = int(body.get("limit", 0))
	except (TypeError, ValueError) as error:
		raise ValueError("The limit has to be a number.") from error
	if mode == "limited" and not 1 <= limit < len(options):
		raise ValueError(f"The limit has to be between 1 and {len(options) - 1}.")

	embed = body.get("embed")
	content = str(body.get("content") or "").strip()
	if owned and not content and not isinstance(embed, dict):
		raise ValueError("Give the panel some text, or an embed.")

	return {
		"title": title[:MAX_TITLE],
		"content": content[:MAX_CONTENT],
		"embed": embed if isinstance(embed, dict) else None,
		"style": style,
		"mode": mode,
		"limit": limit if mode == "limited" else 0,
		"per_row": per_row,
		"options": options,
	}
