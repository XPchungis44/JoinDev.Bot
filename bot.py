"""
bot.py — JoinDev Discord bot skeleton
Python 3.11+ | discord.py 2.4+
"""

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

from welcome import build_welcome_embed

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("joindev")

# ---------------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True       # needed for guild member tracking
intents.message_content = False  # we don't need to read messages
intents.dm_messages = True   # needed for /daily in DMs

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Connected to {len(bot.guilds)} guild(s)")

    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")


# ---------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------
@bot.tree.command(name="ping", description="Check if JoinDev is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**",
        ephemeral=True,
    )


# ---------------------------------------------------------------
# WELCOME DM (called by OAuth callback)
# ---------------------------------------------------------------
async def send_welcome_dm(user_id: int) -> bool:
    """
    Sends the JoinDev welcome embed to a user via DM.

    Args:
        user_id: Discord user ID (from OAuth callback)

    Returns:
        True if sent, False if the user has DMs closed.
    """
    try:
        user = await bot.fetch_user(user_id)
        embed = build_welcome_embed()
        await user.send(embed=embed)
        log.info(f"Welcome DM sent to {user} ({user_id})")
        return True
    except discord.Forbidden:
        log.warning(f"Could not DM user {user_id} — DMs are closed.")
        return False
    except discord.HTTPException as e:
        log.error(f"Failed to send welcome DM to {user_id}: {e}")
        return False


# ---------------------------------------------------------------
# HELPER: MAX JOINABLE SERVERS
# ---------------------------------------------------------------
def max_joinable_servers() -> int:
    """
    Returns how many more servers the user could theoretically join.

    Note: Discord's hard cap for a user is 100 servers.
    This helper is a placeholder — the actual available count is
    calculated per-user in the /auto_join logic (100 - current_guilds).
    """
    DISCORD_USER_GUILD_LIMIT = 100
    return DISCORD_USER_GUILD_LIMIT


# ---------------------------------------------------------------
# RUN
# ---------------------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
