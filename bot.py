"""
bot.py — JoinDev Discord bot with OAuth callback + Postgres
Python 3.11+ | discord.py 2.4+
"""

import os
import logging
import threading

import discord
from discord import app_commands
from discord.ext import commands, tasks

from welcome import build_welcome_embed
from oauth_callback import oauth_app, WELCOME_QUEUE_FILE
from database import init_db

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
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------
# FLASK IN BACKGROUND THREAD
# ---------------------------------------------------------------
def run_flask():
    oauth_app.run(host="0.0.0.0", port=5000, use_reloader=False)


def start_flask():
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("OAuth callback server started on port 5000")


# ---------------------------------------------------------------
# WELCOME QUEUE WATCHER
# ---------------------------------------------------------------
@tasks.loop(seconds=5)
async def watch_welcome_queue():
    """Checks for new user IDs in the queue and sends welcome DMs."""
    if not os.path.exists(WELCOME_QUEUE_FILE):
        return

    try:
        with open(WELCOME_QUEUE_FILE, "r") as f:
            user_ids = [line.strip() for line in f if line.strip()]

        if not user_ids:
            return

        open(WELCOME_QUEUE_FILE, "w").close()  # clear queue

        for user_id in user_ids:
            try:
                user = await bot.fetch_user(int(user_id))
                await user.send(embed=build_welcome_embed())
                log.info(f"Welcome DM sent to {user} ({user_id})")
            except discord.Forbidden:
                log.warning(f"Cannot DM user {user_id} — DMs closed.")
            except discord.HTTPException as e:
                log.error(f"Failed to DM {user_id}: {e}")

    except IOError as e:
        log.error(f"Queue watcher error: {e}")


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

    watch_welcome_queue.start()


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
# RUN
# ---------------------------------------------------------------
if __name__ == "__main__":
    init_db()          # create schema + table on startup
    start_flask()      # start OAuth callback server
    bot.run(TOKEN)
