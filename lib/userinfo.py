from datetime import datetime, timezone

import discord

FLAG_BADGES = {
	"staff": "<:staff:1548054439552749630>",
	"partner": "<:partner:1548054429704396911>",
	"hypesquad": "<:hs_events:1548054414466617449>",
	"hypesquad_bravery": "<:hs_bravery:1548054388734427239>",
	"hypesquad_brilliance": "<:hs_brilliance:1548054403376742410>",
	"hypesquad_balance": "<:hs_balance:1548054378374631605>",
	"bug_hunter": "<:bug_hunter:1548054325455097986>",
	"bug_hunter_level_2": "<:bug_hunter_2:1548054334179123200>",
	"early_supporter": "<:early_sup:1548054353766654093>",
	"early_verified_bot_developer": "<:early_vbot_dev:1548054367003873380>",
	"discord_certified_moderator": "<:certified_mod:1548054344404959333>",
	"active_developer": "<:active_dev:1548054314726068224>",
}

BOOST_TIERS = [
	(24, "<:boost_24:1548054477385371848>"),
	(18, "<:boost_18:1548054476156444723>"),
	(15, "<:boost_15:1548054474071998575>"),
	(12, "<:boost_12:1548054472767316130>"),
	(9, "<:boost_9:1548054470972145815>"),
	(6, "<:boost_6:1548054469521051698>"),
	(3, "<:boost_3:1548054467516047540>"),
	(2, "<:boost_2:1548054466283049000>"),
	(1, "<:boost_1:1548054464626171905>"),
]

def full_size(asset: discord.Asset) -> str:
	"""Get full size asset."""
	return asset.with_size(4096).url

def user_badges(user: discord.User | discord.Member) -> list[str]:
	"""Emoji for every badge the user's public flags say they have."""
	flags = user.public_flags
	return [badge for attr, badge in FLAG_BADGES.items() if getattr(flags, attr, False)]

def boost_badge(member: discord.Member) -> str | None:
	"""Boost badge for how long they've boosted this server,
	isn't necessarily the tier shown on their profile."""
	if not isinstance(member, discord.Member) or member.premium_since is None:
		return None
	days = (datetime.now(timezone.utc) - member.premium_since).days
	months = days // 30
	for threshold, emoji in BOOST_TIERS:
		if months >= threshold:
			return emoji
	return BOOST_TIERS[-1][1]

########## ======================================================================== ##########

MAX_ROLES_SHOWN = 20

def role_list(member: discord.Member) -> str:
	"""Roles highest first, trimmed to fit a TextDisplay."""
	roles = sorted((r for r in member.roles if not r.is_default()), key=lambda r: r.position, reverse=True)
	if not roles:
		return "No roles"

	shown = [r.mention for r in roles[:MAX_ROLES_SHOWN]]
	if len(roles) > MAX_ROLES_SHOWN:
		shown.append(f"*+{len(roles) - MAX_ROLES_SHOWN} more*")
	return " ".join(shown)

def key_permissions(member: discord.Member) -> str:
	"""The permissions worth flagging. Uses Discord's own elevated() set rather than
	our own idea of which ones matter."""
	perms = member.guild_permissions
	if perms.administrator:
		return "**Administrator** (all permissions)"

	names = [name for name, value in perms if value and getattr(discord.Permissions.elevated(), name, False)]
	if not names:
		return "No elevated permissions"
	return ", ".join(n.replace("_", " ").title() for n in sorted(names))

def member_summary(member: discord.Member) -> list[str]:
	"""Role count, top role and colour, for the details block."""
	roles = [r for r in member.roles if not r.is_default()]
	lines = [f"**Roles:** {len(roles)}"]
	if roles:
		lines.append(f"**Top role:** {member.top_role.mention}")
	if member.colour.value:
		lines.append(f"**Color:** `{member.colour}`")
	return lines
