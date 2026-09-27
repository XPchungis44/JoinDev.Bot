"""
bot.py — JoinDev Discord bot (commands + welcome queue watcher)
"""

import os
import logging
import time

import discord
from discord.ext import commands, tasks

from welcome import build_welcome_embed
from oauth_callback import WELCOME_QUEUE_FILE

log = logging.getLogger("joindev.bot")

intents = discord.Intents.default()
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


@tasks.loop(seconds=5)
async def watch_welcome_queue():
    """Reads the welcome queue file and sends DMs to new users."""
    if not os.path.exists(WELCOME_QUEUE_FILE):
        return
    try:
        with open(WELCOME_QUEUE_FILE, "r") as f:
            user_ids = [line.strip() for line in f if line.strip()]
        if not user_ids:
            return
        open(WELCOME_QUEUE_FILE, "w").close()

        for uid in user_ids:
            try:
                user = await bot.fetch_user(int(uid))
                await user.send(embed=build_welcome_embed())
                log.info(f"Welcome DM sent to {uid}")
            except discord.Forbidden:
                log.warning(f"Cannot DM {uid} — DMs closed.")
            except discord.HTTPException as e:
                log.error(f"DM to {uid} failed: {e}")
    except IOError as e:
        log.error(f"Queue read error: {e}")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"In {len(bot.guilds)} guild(s)")
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Slash sync failed: {e}")

    if not watch_welcome_queue.is_running():
        watch_welcome_queue.start()


@bot.tree.command(name="ping", description="Check if JoinDev is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**",
        ephemeral=True,
    )


@bot.tree.command(name="balance", description="Check your JoinCoin balance.")
async def balance(interaction: discord.Interaction):
    from database import get_user
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "You haven't authorized JoinDev yet. Visit the website to get started!",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"🪙 You have **{user['joindev_coins']} JoinCoins**.\n"
        f"🔥 Daily streak: **{user['daily_streak']}**",
        ephemeral=True,
    )
