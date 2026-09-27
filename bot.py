"""
bot.py — JoinDev Discord bot (commands + welcome queue watcher)
"""

import os
import time
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from welcome import build_welcome_embed
from oauth_callback import WELCOME_QUEUE_FILE
from database import (
    get_user,
    claim_daily,
    set_do_not_join,
)

log = logging.getLogger("joindev.bot")

intents = discord.Intents.default()
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------
# WELCOME QUEUE WATCHER
# ---------------------------------------------------------------
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


# ---------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------
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


# ---------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------
@bot.tree.command(name="ping", description="Check if JoinDev is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**",
        ephemeral=True,
    )


@bot.tree.command(name="balance", description="Check your JoinCoin balance.")
async def balance(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "You haven't authorized JoinDev yet. Visit the website to get started!\n"
            "https://xpchungis44.github.io/JoinDev/",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"🪙 You have **{user['joindev_coins']} JoinCoins**.\n"
        f"🔥 Daily streak: **{user['daily_streak']}**",
        ephemeral=True,
    )


@bot.tree.command(name="daily", description="Claim your daily JoinCoins reward.")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send(
            "You haven't authorized JoinDev yet. Visit the website to get started!\n"
            "https://xpchungis44.github.io/JoinDev/",
            ephemeral=True,
        )
        return

    now = int(time.time())
    result = await claim_daily(interaction.user.id, now)

    if not result["success"]:
        if result.get("reason") == "cooldown":
            seconds_left = result["seconds_left"]
            hours = seconds_left // 3600
            minutes = (seconds_left % 3600) // 60
            await interaction.followup.send(
                f"⏳ You've already claimed your daily reward.\n"
                f"Come back in **{hours}h {minutes}m**.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Something went wrong. Try again later.",
            ephemeral=True,
        )
        return

    coins = result["coins_awarded"]
    streak = result["new_streak"]
    balance = result["new_balance"]

    embed = discord.Embed(
        title="🪙 Daily Reward Claimed!",
        color=0x5865F2,
    )
    embed.add_field(name="Coins Earned", value=f"**+{coins}**", inline=True)
    embed.add_field(name="Streak", value=f"🔥 **{streak}** days", inline=True)
    embed.add_field(name="New Balance", value=f"**{balance}** JoinCoins", inline=True)
    embed.set_footer(text="Come back in 24 hours to keep your streak going!")

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel_joinr", description="Stop all pending auto-joins immediately.")
async def cancel_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "You haven't authorized JoinDev yet.",
            ephemeral=True,
        )
        return

    if user["do_not_join"]:
        await interaction.response.send_message(
            "🚫 Auto-joins are already disabled for your account.\n"
            "Use `/resume_joinr` to re-enable them.",
            ephemeral=True,
        )
        return

    await set_do_not_join(interaction.user.id, True)
    await interaction.response.send_message(
        "✅ **Auto-joins disabled.**\n"
        "You will no longer be added to any servers. Use `/resume_joinr` to re-enable.",
        ephemeral=True,
    )


@bot.tree.command(name="resume_joinr", description="Re-enable auto-joins for your account.")
async def resume_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "You haven't authorized JoinDev yet.",
            ephemeral=True,
        )
        return

    if not user["do_not_join"]:
        await interaction.response.send_message(
            "✅ Auto-joins are already active on your account.",
            ephemeral=True,
        )
        return

    await set_do_not_join(interaction.user.id, False)
    await interaction.response.send_message(
        "✅ **Auto-joins re-enabled.** You can now farm JoinCoins again.",
        ephemeral=True,
    )
