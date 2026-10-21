# ==== EXPERIMENTAL CHANGES: may receive constant update for fixes as i push random changes to it! ====

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import timedelta, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


# ==================================
# SETTINGS  (edit these)
# ==================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CAT_API_KEY = os.getenv("CAT_API_KEY")  # optional, only for higher rate limits

BOT_NAME = "Furnace"
VERSION_NUMBER = "1.1-beta1"
PREFIX = "!"
TIMEZONE = "Europe/Bucharest"
DATA_FILE = "furnace_data.json"

# ---- Straight Random Activity Message ----

ACTIVITY_AUTOCHANGE_INTERVAL_MINUTES = 10
RANDOM_ACTIVITY_MESSAGES = [
    "67676767",
    "mrrraoww",
    "Hi",
    "Bye",
    "commando is the best mod ever",
    "N0rvex is my dad",
]

# ---- Daily question ----
DAILY_CHANNEL_ID = 1552389956054425711
DAILY_HOUR = 12
DAILY_MINUTE = 0

# ---- Cat pics ----
# Channel for the cat pics. Leave as None to use the daily question channel.
CAT_CHANNEL_ID = None
CAT_INTERVAL_DAYS = 3
CAT_API_URL = "https://api.thecatapi.com/v1/images/search"

# ---- Moderation ----
# Staff are detected by Discord permission (Ban Members, Moderate Members,
# Kick Members, Manage Channels; Administrators can use everything).
# Optional: also allow specific roles regardless of permissions.
EXTRA_STAFF_ROLES: set[str] = set()

# Admin roles: members with one of these can use ALL staff commands and the
# admin-only commands (!testdaily, !testcat), exactly like a server Administrator.
# This only affects the bot, it doesn't give them any Discord permissions.
ADMIN_ROLES = {
    "Sacrificement Ritual Members",
}

# Roles the VoSR commands can be used ON. Use set() to allow anyone.
TARGET_ROLES = {
    "Victims of Sacrificement Ritual",
}


# ==================================
# STORAGE  (saves data so it survives restarts)
# ==================================

"""Tiny JSON store so state survives restarts."""


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"{self.path} is corrupted, starting with empty data.")
                self.data = {}

    def save(self) -> None:
        # write to a temp file first so a crash can't leave a half-written file
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def ref(self, key: str, default):
        """Returns the stored object itself (creating it if missing).
        If you change it in place, call save() afterwards."""
        return self.data.setdefault(key, default)


# ==================================
# HELPERS
# ==================================

"""Shared helpers: permission checks, time parsing, channel lookup."""


UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIME_RE = re.compile(r"(\d+)\s*([smhdw])")


class NotAllowed(app_commands.CheckFailure):
    """Raised by checks; the message is shown to the user."""


def role_list(names) -> str:
    return ", ".join(f"**{n}**" for n in sorted(names))


def is_admin(member) -> bool:
    """Server Administrator, or one of ADMIN_ROLES."""
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator
        or any(r.name in ADMIN_ROLES for r in member.roles)
    )


def admin_only():
    """Prefix-command check: Administrator permission or an ADMIN_ROLES role."""

    async def predicate(ctx: commands.Context) -> bool:
        if is_admin(ctx.author):
            return True
        raise commands.CheckFailure("You need to be an admin to use this command.")

    return commands.check(predicate)


def has_staff(permission: str):
    """Allows anyone with `permission` (or Administrator), or one of EXTRA_STAFF_ROLES."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise NotAllowed("This command only works in a server.")
        perms = member.guild_permissions
        if (
            is_admin(member)
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


def when(seconds) -> str:
    """Discord relative timestamp for `seconds` from now."""
    if seconds is None:
        return "permanent"
    return f"<t:{int(time.time() + seconds)}:R>"


async def get_channel(bot, channel_id: int):
    """Cache lookup first, then an API fetch. Returns None if it can't be reached."""
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


# ==================================
# MODERATION  (/banvosr /mutevosr /kickvosr /hidechannelvosr)
# ==================================

MAX_TIMEOUT = 28 * 86400  # Discord's timeout limit


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


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # temporary bans / hidden channels waiting to expire
        self.punishments: list[dict] = bot.store.ref("punishments", [])
        self._migrate_old_file()

    def _migrate_old_file(self):
        """Pulls pending timers from the old punishments.json (pre-rework) once."""
        legacy = Path("punishments.json")
        if not legacy.exists():
            return
        try:
            self.punishments.extend(json.loads(legacy.read_text(encoding="utf-8")))
            self.bot.store.save()
            legacy.rename("punishments.json.migrated")
        except (OSError, ValueError):
            pass

    async def cog_load(self):
        self.expiry_loop.start()

    def cog_unload(self):
        self.expiry_loop.cancel()

    def schedule(self, kind: str, guild_id: int, user_id: int, seconds, channel_id=None):
        if seconds is None:
            return
        self.punishments.append(
            {
                "type": kind,
                "guild": guild_id,
                "user": user_id,
                "channel": channel_id,
                "expires": time.time() + seconds,
            }
        )
        self.bot.store.save()

    # ---------- commands ----------

    @app_commands.command(name="banvosr", description="Ban a Victim of Sacrificement Ritual")
    @app_commands.describe(
        user="User to ban",
        duration="How long, e.g. 30m, 2h, 7d, or perm (default: perm)",
        reason="Why they're being banned",
    )
    @app_commands.rename(duration="time")
    @app_commands.guild_only()
    @has_staff("ban_members")
    async def banvosr(
        self,
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

        self.schedule("ban", interaction.guild.id, user.id, seconds)
        await interaction.followup.send(f"🔨 **{user}** was banned ({when(seconds)}).\n**Reason:** {reason}")

    @app_commands.command(name="mutevosr", description="Mute (timeout) a Victim of Sacrificement Ritual")
    @app_commands.describe(
        user="User to mute",
        duration="How long, e.g. 10m, 2h, 7d (max 28d)",
        reason="Why they're being muted",
    )
    @app_commands.rename(duration="time")
    @app_commands.guild_only()
    @has_staff("moderate_members")
    async def mutevosr(
        self,
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

        await interaction.response.send_message(f"🔇 **{user}** was muted (ends {when(seconds)}).\n**Reason:** {reason}")

    @app_commands.command(name="kickvosr", description="Kick a Victim of Sacrificement Ritual")
    @app_commands.describe(user="User to kick", reason="Why they're being kicked")
    @app_commands.guild_only()
    @has_staff("kick_members")
    async def kickvosr(
        self,
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

    @app_commands.command(name="hidechannelvosr", description="Hide a channel from a Victim of Sacrificement Ritual")
    @app_commands.describe(
        user="User to hide the channel from",
        duration="How long, e.g. 30m, 2h, 7d, or perm (default: perm)",
        reason="Why the channel is being hidden",
        channel="Channel to hide (default: this channel)",
    )
    @app_commands.rename(duration="time")
    @app_commands.guild_only()
    @has_staff("manage_channels")
    async def hidechannelvosr(
        self,
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

        self.schedule("hide", interaction.guild.id, user.id, seconds, channel_id=channel.id)
        await interaction.response.send_message(
            f"🙈 {channel.mention} is now hidden from **{user}** ({when(seconds)}).\n**Reason:** {reason}",
            ephemeral=True,
        )

    # ---------- expiry of temporary bans / hides ----------

    @tasks.loop(seconds=30)
    async def expiry_loop(self):
        now = time.time()
        due = [p for p in self.punishments if p["expires"] <= now]

        for p in due:
            guild = self.bot.get_guild(p["guild"])
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
            self.punishments.remove(p)

        if due:
            self.bot.store.save()

    @expiry_loop.before_loop
    async def before_expiry_loop(self):
        await self.bot.wait_until_ready()


# ==================================
# PING REPLIES + !ping
# ==================================

PING_RESPONSES = [
    "🔥 You rang? {name}, I'm warmed up and ready.",
    "Furnace online. What do you need, {name}?",
    "You called? I was just keeping the server warm.",
    "Ping received. Fire's lit, how can I help?",
    "🔥 Hot take: try `/help` and see what I can do.",
    "I was napping by the fireplace... what's up, {name}?",
    "Did someone say Furnace? 👀",
    "Don't poke the fire! ...kidding, hi 👋",
    "I run hot, but I'm friendly. What's going on?",
    "Yes, {name}? I'm all ears (and a little bit of smoke).",
    "Another ping? You're really turning up the heat today.",
    "Present! Keeping things toasty. 🔥",
    # ---- more replies ----
    "Hey {name}, what can I do for you?",
    "Yep, I'm here. What's up?",
    "You pinged, I answered. Impressive teamwork.",
    "Hello, {name}! Hope your day's going well.",
    "Need something? Just say the word.",
    "I'm awake, I promise. What's going on?",
    "At your service, {name}. 🔥",
    "Ping! Pong! ...wait, wrong game.",
    "Somebody say my name? Hi there!",
    "Always here, always warm. What do you need?",
    "Hi {name}! Try `/help` if you're not sure where to start.",
    "You've got my attention. Go ahead!",
    "Back from the boiler room. What did I miss?",
    "Heard you loud and clear, {name}.",
    "That's me! How's it going?",
    "Reporting for duty. What's the plan?",
    "Quick question: tea or coffee? Anyway, what's up?",
    "Turning the heat up just for you, {name}. 🔥",
]

PING_COOLDOWN = 5  # seconds per user, so nobody can spam-ping it


EIGHT_BALL = [
    "Yes, definitely. 🔥",
    "It is certain.",
    "Without a doubt.",
    "Signs point to yes.",
    "Most likely.",
    "Ask again later.",
    "Hmm, hard to say right now.",
    "Better not tell you now.",
    "Don't count on it.",
    "My sources say no.",
    "Very doubtful.",
    "Absolutely not.",
]


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_reply: dict[int, float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.content.startswith(PREFIX):
            return  # it's a command, not a ping chat
        if self.bot.user not in message.mentions:
            return

        now = time.monotonic()
        if now - self._last_reply.get(message.author.id, 0) < PING_COOLDOWN:
            return
        self._last_reply[message.author.id] = now

        reply = random.choice(PING_RESPONSES).format(name=message.author.display_name)
        await message.reply(reply, mention_author=False)

    @commands.command()
    async def ping(self, ctx: commands.Context):
        await ctx.send("pong")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question")
    @app_commands.describe(question="Your yes/no question")
    @app_commands.guild_only()
    async def eight_ball(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 200],
    ):
        await interaction.response.send_message(
            f"🎱 **{question}**\n{random.choice(EIGHT_BALL)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ==================================
# CAT PICTURES  (every few days)
# ==================================

class Cats(commands.Cog):
    """Posts a fresh cat picture every CAT_INTERVAL_DAYS days."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.cat_loop.start()

    def cog_unload(self):
        self.cat_loop.cancel()

    async def fetch_cat(self):
        """Returns an image URL from the cat API, or None if it failed."""
        headers = {"x-api-key": CAT_API_KEY} if CAT_API_KEY else {}
        try:
            async with self.bot.session.get(
                CAT_API_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    print(f"Cat API returned status {resp.status}")
                    return None
                data = await resp.json()
                return data[0]["url"]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, ValueError) as e:
            print(f"Cat API request failed: {e}")
            return None

    async def post_cat(self, reset_timer: bool = True) -> bool:
        channel = await get_channel(self.bot, CAT_CHANNEL_ID or DAILY_CHANNEL_ID)
        if channel is None:
            print("Cat channel not found!")
            return False

        url = await self.fetch_cat()
        if url is None:
            return False

        embed = discord.Embed(title="🐱 Fresh cat delivery", color=discord.Color.orange())
        embed.set_image(url=url)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print("I'm not allowed to send messages in the cat channel!")
            return False

        if reset_timer:
            self.bot.store.set("last_cat_post", time.time())
        print("Cat picture posted successfully!")
        return True

    @tasks.loop(minutes=30)
    async def cat_loop(self):
        # Checking every 30 minutes against a saved timestamp (instead of one
        # 72h timer) means restarting the bot doesn't reset the 3-day schedule.
        last = self.bot.store.get("last_cat_post")
        interval = CAT_INTERVAL_DAYS * 86400
        if last is None or time.time() - last >= interval:
            await self.post_cat()

    @cat_loop.before_loop
    async def before_cat_loop(self):
        await self.bot.wait_until_ready()

    @commands.command()
    @admin_only()
    async def testcat(self, ctx: commands.Context):
        """Posts a cat picture right now (admins only). Doesn't affect the schedule."""
        if await self.post_cat(reset_timer=False):
            await ctx.send("✅ Cat picture sent.")
        else:
            await ctx.send("❌ Couldn't post a cat picture. Check the console for the reason.")


# ==================================
# DAILY QUESTION  (+ !testdaily)
# ==================================

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
    "👋 Say hi to someone in the server!",
]


class Daily(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.daily_message.start()

    def cog_unload(self):
        self.daily_message.cancel()

    async def send_daily_message(self) -> bool:
        """Posts a random daily question. Returns True if it was sent."""
        channel = await get_channel(self.bot, DAILY_CHANNEL_ID)
        if channel is None:
            print("Daily message channel not found!")
            return False

        try:
            await channel.send(f"📅 **Daily Question**\n\n{random.choice(DAILY_MESSAGES)}")
        except discord.Forbidden:
            print("I'm not allowed to send messages in the daily channel!")
            return False

        print("Daily message sent successfully!")
        return True

    @tasks.loop(
        time=datetime_time(
            hour=DAILY_HOUR,
            minute=DAILY_MINUTE,
            tzinfo=ZoneInfo(TIMEZONE),
        )
    )
    async def daily_message(self):
        await self.send_daily_message()

    @daily_message.before_loop
    async def before_daily_message(self):
        await self.bot.wait_until_ready()

    @commands.command()
    @admin_only()
    async def testdaily(self, ctx: commands.Context):
        """Sends a daily question right now (admins only)."""
        if await self.send_daily_message():
            await ctx.send("✅ Daily question sent.")
        else:
            await ctx.send("❌ Couldn't send it. Check the daily channel ID and my permissions there.")


# ==================================
# HELP  (/help and !help)
# ==================================

def build_help_embed() -> discord.Embed:
    p = PREFIX
    embed = discord.Embed(
        title=f"🔥 {BOT_NAME} help",
        description="Here's everything I can do.",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="For everyone",
        value=(
            "**Ping me** - I'll answer with something random\n"
            "`/help` or `" + p + "help` - show this message\n"
            "`/8ball question` - ask the magic 8-ball\n"
            "`" + p + "ping` - check that I'm alive\n"
            f"🐱 I also post a fresh cat picture every {CAT_INTERVAL_DAYS} days\n"
            "📅 and a daily question at 12:00 (Bucharest time)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Staff (slash commands)",
        value=(
            "`/banvosr user [time] [reason]` - needs Ban Members\n"
            "`/mutevosr user time [reason]` - needs Moderate Members\n"
            "`/kickvosr user [reason]` - needs Kick Members\n"
            "`/hidechannelvosr user [time] [reason] [channel]` - needs Manage Channels\n"
            "Admin roles can use all of these. Time looks like `30m`, `2h`, `7d`, `1d12h` or `perm`."
        ),
        inline=False,
    )
    embed.add_field(
        name="Admins",
        value=(
            "`" + p + "testdaily` - send the daily question now\n"
            "`" + p + "testcat` - post a cat picture now"
        ),
        inline=False,
    )
    return embed


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show what Furnace can do")
    async def help_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

    @commands.command(name="help")
    async def help_prefix(self, ctx: commands.Context):
        await ctx.send(embed=build_help_embed())


# ==================================
# BOT + RUN
# ==================================

class Furnace(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # must ALSO be enabled in the Developer Portal

        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,  # we have our own !help (see HELP section)
        )
        self.store = Store(DATA_FILE)
        self.session: aiohttp.ClientSession | None = None
        self.tree.error(self.on_tree_error)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        for cog in (Moderation, Fun, Cats, Daily, Help):
            await self.add_cog(cog(self))
        synced = await self.tree.sync()
        self.change_status.start()
        print(f"Synced {len(synced)} slash commands")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")

    @tasks.loop(minutes=ACTIVITY_AUTOCHANGE_INTERVAL_MINUTES)
    async def change_status(self):
        random_msg = random.choice(RANDOM_ACTIVITY_MESSAGES)
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{random_msg} // {VERSION_NUMBER}"
        )
        await self.change_presence(activity=activity)

    @change_status.before_loop
    async def before_change_status(self):
        await self.wait_until_ready()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        await ctx.send(f"Error: {error}")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, NotAllowed):
            message = str(error)
        else:
            logging.getLogger("discord").exception("Command error", exc_info=error)
            message = "Something went wrong running that command."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Add it to your .env file.")

    handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
    Furnace().run(TOKEN, log_handler=handler, log_level=logging.INFO)
