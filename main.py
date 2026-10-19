import json
import logging
import os
import random
import re
import time
from datetime import timedelta, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ==============================
# SETTINGS
# ==============================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DAILY_CHANNEL_ID =1536064553077448846
VERSION = "1.0-beta6" # Update the version number when committing changes, if this is on experimental.py, use "beta" or "alpha" so i can know this code is gonna be flaky

# Staff are detected automatically by Discord permission:
#   /banvosr -> Ban Members | /mutevosr -> Moderate Members
#   /kickvosr -> Kick Members | /hidechannelvosr -> Manage Channels
# Administrators can use all of them.
# Optional: also allow specific roles regardless of permissions, e.g. {"Moderator", "Helper"}
EXTRA_STAFF_ROLES = {
    "Sacrificement Ritual Members",
}

# Roles the commands can be used ON (the target needs any one of them).
# Leave empty ( set() ) to allow targeting anyone, apart from the safety checks.
TARGET_ROLES = {
    "Victims of Sacrificement Ritual",
}

DATA_FILE = Path("punishments.json")
punishments: list[dict] = []  # temporary bans / hidden channels waiting to expire

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # must ALSO be enabled in the Developer Portal


class N0rvexBuddy(commands.Bot):
    async def setup_hook(self):
        load_punishments()
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash commands")
        expiry_loop.start()
        daily_message.start()
        ping_status_loop.start()


bot = N0rvexBuddy(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"Error: {error}")


# ==============================
# HELPERS
# ==============================

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIME_RE = re.compile(r"(\d+)\s*([smhdw])")
MAX_TIMEOUT = 28 * 86400  # Discord's timeout limit


class NotAllowed(app_commands.CheckFailure):
    pass


def role_list(names) -> str:
    return ", ".join(f"**{n}**" for n in sorted(names))


def has_staff(permission: str):
    """Allows anyone with `permission` (or Administrator), or one of EXTRA_STAFF_ROLES."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise NotAllowed("This command only works in a server.")
        perms = member.guild_permissions
        if (
            perms.administrator
            or getattr(perms, permission)
            or any(r.name in EXTRA_STAFF_ROLES for r in member.roles)
        ):
            return True
        nice = permission.replace("_", " ").title()
        raise NotAllowed(f"You need the **{nice}** permission to use this command.")

    return app_commands.check(predicate)


def parse_duration(text: str):
    """'10m', '2h', '1d12h', '1w' -> seconds. 'perm' -> None. Bad input -> ValueError."""
    text = text.strip().lower()
    if text in ("perm", "permanent", "forever"):
        return None
    if not TIME_RE.search(text) or TIME_RE.sub("", text).strip():
        raise ValueError
    return sum(int(n) * UNITS[u] for n, u in TIME_RE.findall(text))


def check_target(interaction: discord.Interaction, target: discord.Member):
    """Returns an error message, or None if the target is valid."""
    if target.id == interaction.user.id:
        return "You can't use this command on yourself."
    if TARGET_ROLES and not any(r.name in TARGET_ROLES for r in target.roles):
        return f"You can only use this on users with one of these roles: {role_list(TARGET_ROLES)}."
    if target.top_role >= interaction.guild.me.top_role:
        return "I can't act on that user because their top role is higher than or equal to mine."
    return None


def audit_reason(interaction: discord.Interaction, reason: str) -> str:
    return f"{reason} | by {interaction.user} ({interaction.user.id})"[:512]


def when(seconds):
    if seconds is None:
        return "permanent"
    return f"<t:{int(time.time() + seconds)}:R>"


def load_punishments():
    global punishments
    if DATA_FILE.exists():
        punishments = json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save_punishments():
    DATA_FILE.write_text(json.dumps(punishments, indent=2), encoding="utf-8")


def schedule(kind: str, guild_id: int, user_id: int, seconds, channel_id=None):
    if seconds is None:
        return
    punishments.append(
        {
            "type": kind,
            "guild": guild_id,
            "user": user_id,
            "channel": channel_id,
            "expires": time.time() + seconds,
        }
    )
    save_punishments()


# ==============================
# VoSR SLASH COMMANDS
# ==============================

@bot.tree.command(name="banvosr", description="Ban a Victim of Sacrificement Ritual")
@app_commands.describe(
    user="User to ban",
    duration="How long, e.g. 30m, 2h, 7d, or perm (default: perm)",
    reason="Why they're being banned",
)
@app_commands.rename(duration="time")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@has_staff("ban_members")
async def banvosr(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str = "perm",
    reason: app_commands.Range[str, 1, 400] = "No reason provided",
):
    if (err := check_target(interaction, user)):
        return await interaction.response.send_message(err, ephemeral=True)
    try:
        seconds = parse_duration(duration)
    except ValueError:
        return await interaction.response.send_message(
            "Invalid time. Use things like `30m`, `2h`, `7d`, `1d12h` or `perm`.", ephemeral=True
        )

    await interaction.response.defer()
    try:
        await interaction.guild.ban(user, reason=audit_reason(interaction, reason), delete_message_seconds=0)
    except discord.Forbidden:
        return await interaction.followup.send("I don't have permission to ban that user.", ephemeral=True)

    schedule("ban", interaction.guild.id, user.id, seconds)
    await interaction.followup.send(
        f"🔨 **{user}** was banned ({when(seconds)}).\n**Reason:** {reason}"
    )


@bot.tree.command(name="mutevosr", description="Mute (timeout) a Victim of Sacrificement Ritual")
@app_commands.describe(
    user="User to mute",
    duration="How long, e.g. 10m, 2h, 7d (max 28d)",
    reason="Why they're being muted",
)
@app_commands.rename(duration="time")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@has_staff("moderate_members")
async def mutevosr(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: app_commands.Range[str, 1, 400] = "No reason provided",
):
    if (err := check_target(interaction, user)):
        return await interaction.response.send_message(err, ephemeral=True)
    try:
        seconds = parse_duration(duration)
    except ValueError:
        seconds = None
    if seconds is None or seconds <= 0 or seconds > MAX_TIMEOUT:
        return await interaction.response.send_message(
            "Mutes need a time between `1s` and `28d`, e.g. `10m`, `2h`, `7d`.", ephemeral=True
        )

    try:
        await user.timeout(timedelta(seconds=seconds), reason=audit_reason(interaction, reason))
    except discord.Forbidden:
        return await interaction.response.send_message("I don't have permission to mute that user.", ephemeral=True)

    await interaction.response.send_message(
        f"🔇 **{user}** was muted (ends {when(seconds)}).\n**Reason:** {reason}"
    )


@bot.tree.command(name="kickvosr", description="Kick a Victim of Sacrificement Ritual")
@app_commands.describe(user="User to kick", reason="Why they're being kicked")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@has_staff("kick_members")
async def kickvosr(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: app_commands.Range[str, 1, 400] = "No reason provided",
):
    if (err := check_target(interaction, user)):
        return await interaction.response.send_message(err, ephemeral=True)

    try:
        await user.kick(reason=audit_reason(interaction, reason))
    except discord.Forbidden:
        return await interaction.response.send_message("I don't have permission to kick that user.", ephemeral=True)

    await interaction.response.send_message(f"👢 **{user}** was kicked.\n**Reason:** {reason}")


@bot.tree.command(name="hidechannelvosr", description="Hide a channel from a Victim of Sacrificement Ritual")
@app_commands.describe(
    user="User to hide the channel from",
    duration="How long, e.g. 30m, 2h, 7d, or perm (default: perm)",
    reason="Why the channel is being hidden",
    channel="Channel to hide (default: this channel)",
)
@app_commands.rename(duration="time")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@has_staff("manage_channels")
async def hidechannelvosr(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str = "perm",
    reason: app_commands.Range[str, 1, 400] = "No reason provided",
    channel: discord.abc.GuildChannel | None = None,
):
    if (err := check_target(interaction, user)):
        return await interaction.response.send_message(err, ephemeral=True)
    try:
        seconds = parse_duration(duration)
    except ValueError:
        return await interaction.response.send_message(
            "Invalid time. Use things like `30m`, `2h`, `7d`, `1d12h` or `perm`.", ephemeral=True
        )

    channel = channel or interaction.channel
    try:
        await channel.set_permissions(user, view_channel=False, reason=audit_reason(interaction, reason))
    except discord.Forbidden:
        return await interaction.response.send_message(
            "I don't have permission to edit that channel's permissions.", ephemeral=True
        )

    schedule("hide", interaction.guild.id, user.id, seconds, channel_id=channel.id)
    await interaction.response.send_message(
        f"🙈 {channel.mention} is now hidden from **{user}** ({when(seconds)}).\n**Reason:** {reason}",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, NotAllowed):
        message = str(error)
    else:
        logging.getLogger("discord").exception("Command error", exc_info=error)
        message = "Something went wrong running that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ==============================
# TEMP BAN / HIDE EXPIRY
# ==============================

@tasks.loop(seconds=30)
async def expiry_loop():
    now = time.time()
    due = [p for p in punishments if p["expires"] <= now]

    for p in due:
        guild = bot.get_guild(p["guild"])
        if guild is not None:
            target = discord.Object(id=p["user"])
            try:
                if p["type"] == "ban":
                    await guild.unban(target, reason="Temporary ban expired")
                elif p["type"] == "hide":
                    channel = guild.get_channel(p["channel"])
                    if channel is not None:
                        overwrite = channel.overwrites_for(target)
                        overwrite.view_channel = None
                        await channel.set_permissions(
                            target,
                            overwrite=None if overwrite.is_empty() else overwrite,
                            reason="Temporary channel hide expired",
                        )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"Could not finish expiring {p}: {e}")
        punishments.remove(p)

    if due:
        save_punishments()


@expiry_loop.before_loop
async def before_expiry_loop():
    await bot.wait_until_ready()


# ==============================
# DAILY MESSAGE SYSTEM
# ==============================

DAILY_MESSAGES = [
    "🐾 Are you a furry?",
    "🎮 What game are you playing right now?",
    "🌙 Are you a night owl or an early bird?",
    "🐱 Cats or dogs? Choose wisely.",
    "🎵 What song are you listening to today?",
    "👀 What's your most controversial gaming opinion?",
    "💬 Describe your day in one word.",
    "🦊 If you could be any animal for a day, which one?",
    "🔥 What's one game everyone should try?",
    "✨ Daily check-in! How are you doing today?",
    "🤔 What's a skill you'd love to learn?",
    "🎧 Headphones or speakers?",
    "💻 What's your dream PC build?",
    "🐺 What's your favorite animal?",
    "👋 Say hi to someone in the server!"
]


async def send_daily_message() -> bool:
    """Posts a random daily question. Returns True if it was sent."""
    channel = bot.get_channel(DAILY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DAILY_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            print("Daily message channel not found!")
            return False

    message = random.choice(DAILY_MESSAGES)

    try:
        await channel.send(f"📅 **Daily Question**\n\n{message}")
    except discord.Forbidden:
        print("I'm not allowed to send messages in the daily channel!")
        return False

    print("Daily message sent successfully!")
    return True


@tasks.loop(
    time=datetime_time(
        hour=12,
        minute=0,
        tzinfo=ZoneInfo("Europe/Bucharest")
    )
)
async def daily_message():
    await send_daily_message()


@daily_message.before_loop
async def before_daily_message():
    await bot.wait_until_ready()


# ==============================
# PREFIX COMMANDS (!)
# ==============================

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong")


@bot.command()
@commands.has_permissions(administrator=True)
async def testdaily(ctx: commands.Context):
    """Sends a daily question right now (admins only)."""
    ok = await send_daily_message()
    if ok:
        await ctx.send("✅ Daily question sent.")
    else:
        await ctx.send("❌ Couldn't send it. Check the daily channel ID and my permissions there.")

# ==============================
# NETWORK PING DEBUG / INFO STATUS
# ==============================

RANDOM_STATUS = [
    "meow",
    "Hi!!",
    "99% vibecoded",
    "Bye!!",
]

@tasks.loop(minutes=5)
async def ping_status_loop():
    randomtextstatus = random.choice(RANDOM_STATUS)
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{randomtextstatus} // {VERSION}"
        )
    )

@ping_status_loop.before_loop
async def before_ping_status_loop():
    await bot.wait_until_ready()

# ==============================
# RUN
# ==============================

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
