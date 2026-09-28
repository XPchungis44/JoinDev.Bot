"""
bot.py — JoinDev Discord bot (commands + background tasks)
"""

import os
import json
import time
import logging

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from welcome import build_welcome_embed
from oauth_callback import WELCOME_QUEUE_FILE, TOKEN_QUEUE_FILE
from database import (
    init_pool,
    upsert_user,
    get_user,
    claim_daily,
    set_do_not_join,
    add_server,
    get_active_servers,
    create_order,
    increment_order_completed,
    complete_order,
    create_active_join,
    get_pending_joins,
    mark_join_rewarded,
    mark_join_left_early,
    add_coins,
    get_all_users,
    update_tokens,
    THREE_DAYS,
    _pool,
)

log = logging.getLogger("joindev.bot")

intents = discord.Intents.default()
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

INSTALL_URL = (
    "https://discord.com/oauth2/authorize"
    "?client_id=1552458677821382656"
    "&scope=bot+applications.commands"
    "&permissions=8"
)


# ---------------------------------------------------------------
# SETUP HOOK — runs on the bot's event loop before connecting
# ---------------------------------------------------------------
@bot.event
async def setup_hook():
    log.info("Initializing database pool on bot's event loop...")
    await init_pool()
    log.info("Database pool ready.")


# ---------------------------------------------------------------
# QUEUE WATCHER — handles both token storage and welcome DMs
# ---------------------------------------------------------------
@tasks.loop(seconds=5)
async def watch_welcome_queue():
    # ---- Process token queue first (DB writes on bot's loop) ----
    if os.path.exists(TOKEN_QUEUE_FILE):
        try:
            with open(TOKEN_QUEUE_FILE, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                open(TOKEN_QUEUE_FILE, "w").close()

            for line in lines:
                try:
                    entry = json.loads(line)
                    await upsert_user(
                        user_id=entry["user_id"],
                        access_token=entry["access_token"],
                        refresh_token=entry["refresh_token"],
                        expires_at=entry["expires_at"],
                    )
                    log.info(f"Stored tokens for user {entry['user_id']}")
                except json.JSONDecodeError as e:
                    log.error(f"Bad token JSON: {e}")
                except Exception as e:
                    log.error(f"Failed to store tokens: {e}")
        except IOError as e:
            log.error(f"Token queue read error: {e}")

    # ---- Process welcome DM queue ----
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
# 3-DAY JOIN REWARD CHECKER
# ---------------------------------------------------------------
@tasks.loop(hours=1)
async def check_join_rewards():
    pending = await get_pending_joins()
    if not pending:
        return

    log.info(f"Checking {len(pending)} pending joins for 3-day completion")

    for join in pending:
        user_id = join["user_id"]
        guild_id = join["guild_id"]
        order_id = join["order_id"]

        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                await mark_join_left_early(join["id"])
                log.warning(f"Bot not in guild {guild_id}, cannot verify join {join['id']}")
                continue

            member = guild.get_member(user_id)
            if member is None:
                await mark_join_left_early(join["id"])
                log.info(f"User {user_id} left {guild_id} early — no reward")
                continue

            await mark_join_rewarded(join["id"])
            new_balance = await add_coins(user_id, 1)
            log.info(f"User {user_id} rewarded +1 coin (balance {new_balance})")

            try:
                user = await bot.fetch_user(user_id)
                await user.send(
                    f"🪙 **+1 JoinCoin** — You stayed in **{guild.name}** for 3 full days!\n"
                    f"New balance: **{new_balance}** JoinCoins."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

            if order_id:
                updated = await increment_order_completed(order_id)
                if updated and updated["members_completed"] >= updated["members_requested"]:
                    await complete_order(order_id, int(time.time()))
                    log.info(f"Order {order_id} fully completed")

                    try:
                        owner = await bot.fetch_user(updated["owner_id"])
                        await owner.send(
                            f"✅ **Your order is complete!**\n"
                            f"All **{updated['members_requested']}** members have stayed in "
                            f"your server for 3 full days.\n"
                            f"The bot will now leave your server."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                    await _leave_guild(guild_id)

        except Exception as e:
            log.error(f"Error processing join {join['id']}: {e}")


async def _leave_guild(guild_id: int):
    guild = bot.get_guild(guild_id)
    if guild:
        try:
            await guild.leave()
            log.info(f"Bot left guild {guild_id} after order completion")
        except discord.HTTPException as e:
            log.error(f"Failed to leave guild {guild_id}: {e}")


# ---------------------------------------------------------------
# TOKEN REFRESH TASK
# ---------------------------------------------------------------
@tasks.loop(hours=12)
async def refresh_tokens():
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    now = int(time.time())

    users = await get_all_users()
    for u in users:
        if not u["token_expires_at"]:
            continue
        if u["token_expires_at"] - now > 86400:
            continue

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": u["refresh_token"],
        }
        try:
            r = requests.post(
                "https://discord.com/api/v10/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            r.raise_for_status()
            new = r.json()
            await update_tokens(
                u["user_id"],
                new["access_token"],
                new["refresh_token"],
                int(time.time()) + new.get("expires_in", 604800),
            )
            log.info(f"Refreshed tokens for {u['user_id']}")
        except Exception as e:
            log.error(f"Refresh failed for {u['user_id']}: {e}")


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
    if not check_join_rewards.is_running():
        check_join_rewards.start()
    if not refresh_tokens.is_running():
        refresh_tokens.start()


# ---------------------------------------------------------------
# BASIC COMMANDS
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

    result = await claim_daily(interaction.user.id, int(time.time()))
    if not result["success"]:
        if result.get("reason") == "cooldown":
            s = result["seconds_left"]
            await interaction.followup.send(
                f"⏳ Already claimed. Come back in **{s // 3600}h {(s % 3600) // 60}m**.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Something went wrong.", ephemeral=True)
        return

    embed = discord.Embed(title="🪙 Daily Reward Claimed!", color=0x5865F2)
    embed.add_field(name="Coins Earned", value=f"**+{result['coins_awarded']}**", inline=True)
    embed.add_field(name="Streak", value=f"🔥 **{result['new_streak']}** days", inline=True)
    embed.add_field(name="New Balance", value=f"**{result['new_balance']}** JoinCoins", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel_joinr", description="Stop all pending auto-joins immediately.")
async def cancel_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("You haven't authorized JoinDev yet.", ephemeral=True)
        return
    if user["do_not_join"]:
        await interaction.response.send_message(
            "🚫 Auto-joins are already disabled. Use `/resume_joinr` to re-enable.",
            ephemeral=True,
        )
        return
    await set_do_not_join(interaction.user.id, True)
    await interaction.response.send_message(
        "✅ **Auto-joins disabled.** Use `/resume_joinr` to re-enable.",
        ephemeral=True,
    )


@bot.tree.command(name="resume_joinr", description="Re-enable auto-joins for your account.")
async def resume_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("You haven't authorized JoinDev yet.", ephemeral=True)
        return
    if not user["do_not_join"]:
        await interaction.response.send_message(
            "✅ Auto-joins are already active.",
            ephemeral=True,
        )
        return
    await set_do_not_join(interaction.user.id, False)
    await interaction.response.send_message(
        "✅ **Auto-joins re-enabled.** You can farm JoinCoins again.",
        ephemeral=True,
    )


# ---------------------------------------------------------------
# SERVER SUBMISSION
# ---------------------------------------------------------------
@bot.tree.command(name="submit_server", description="Add your server to the JoinDev pool.")
@app_commands.describe(invite="A permanent invite link to your server")
async def submit_server(interaction: discord.Interaction, invite: str):
    if not interaction.guild:
        await interaction.response.send_message(
            "Run this command inside the server you want to submit.",
            ephemeral=True,
        )
        return

    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(
            "Only the server owner can submit this server.",
            ephemeral=True,
        )
        return

    if not invite.startswith("https://discord.gg/") and not invite.startswith("https://discord.com/invite/"):
        await interaction.response.send_message(
            "That doesn't look like a valid Discord invite link.",
            ephemeral=True,
        )
        return

    await add_server(
        guild_id=interaction.guild.id,
        owner_id=interaction.user.id,
        invite_code=invite,
        now=int(time.time()),
    )

    embed = discord.Embed(
        title="✅ Server Added to Pool!",
        description=(
            "**Add me to that server by clicking here → [Install Link]("
            + INSTALL_URL
            + ")**\n\n"
            "Then your order will be complete!"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="What happens next?",
        value=(
            "Once I'm in your server, run `/buy_members` to place an order.\n"
            "I'll add real members and they'll stay for 3 full days.\n"
            "When everyone's done, I'll leave your server automatically."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------
# AUTO JOIN
# ---------------------------------------------------------------
@bot.tree.command(name="auto_join", description="Spend JoinCoins to farm servers.")
@app_commands.describe(amount="How many JoinCoins to spend, or 'max' to spend everything")
async def auto_join(interaction: discord.Interaction, amount: str):
    await interaction.response.defer(ephemeral=True)

    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send(
            "You haven't authorized JoinDev yet. Visit the website to get started!\n"
            "https://xpchungis44.github.io/JoinDev/",
            ephemeral=True,
        )
        return

    if user["do_not_join"]:
        await interaction.followup.send(
            "🚫 Auto-joins are disabled on your account. Run `/resume_joinr` first.",
            ephemeral=True,
        )
        return

    balance = user["joindev_coins"]
    if balance <= 0:
        await interaction.followup.send(
            "🪙 You don't have any JoinCoins. Run `/daily` to earn some.",
            ephemeral=True,
        )
        return

    if amount.lower() == "max":
        to_join = balance
    else:
        try:
            to_join = int(amount)
        except ValueError:
            await interaction.followup.send("Please provide a number or 'max'.", ephemeral=True)
            return
        if to_join <= 0:
            await interaction.followup.send("Amount must be at least 1.", ephemeral=True)
            return
        if to_join > balance:
            await interaction.followup.send(
                f"You only have **{balance}** JoinCoins.", ephemeral=True,
            )
            return

    servers = await get_active_servers()
    if not servers:
        await interaction.followup.send(
            "📭 No servers available in the pool right now. Try again later.",
            ephemeral=True,
        )
        return

    to_join = min(to_join, len(servers))
    servers = servers[:to_join]

    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1
        """, interaction.user.id, to_join)

    joined = 0
    failed = 0
    for srv in servers:
        try:
            guild = bot.get_guild(srv["guild_id"])
            if not guild:
                failed += 1
                continue
            await guild.add_member(interaction.user, reason="JoinDev auto-join")
            await create_active_join(
                user_id=interaction.user.id,
                guild_id=srv["guild_id"],
                order_id=None,
                now=int(time.time()),
            )
            joined += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"Failed to join {srv['guild_id']}: {e}")
            failed += 1

    if failed:
        await add_coins(interaction.user.id, failed)

    await interaction.followup.send(
        f"✅ Joined **{joined}** server(s). Stay in each for **3 full days** to earn "
        f"**1 JoinCoin** back per server.\n"
        f"Coins spent: **{joined}**\n"
        f"Failed: **{failed}** (refunded)",
        ephemeral=True,
    )


# ---------------------------------------------------------------
# BUY MEMBERS
# ---------------------------------------------------------------
@bot.tree.command(name="buy_members", description="Spend JoinCoins to bring members to your server.")
@app_commands.describe(amount="How many members you want, or 'max'")
async def buy_members(interaction: discord.Interaction, amount: str):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild:
        await interaction.followup.send("Run this in the server you want to grow.", ephemeral=True)
        return

    if interaction.user.id != interaction.guild.owner_id:
        await interaction.followup.send(
            "Only the server owner can place orders.", ephemeral=True,
        )
        return

    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send(
            "You haven't authorized JoinDev yet. Visit the website to get started!\n"
            "https://xpchungis44.github.io/JoinDev/",
            ephemeral=True,
        )
        return

    balance = user["joindev_coins"]
    if balance <= 0:
        await interaction.followup.send(
            "🪙 You don't have any JoinCoins. Run `/daily` to earn some.",
            ephemeral=True,
        )
        return

    if amount.lower() == "max":
        to_order = balance
    else:
        try:
            to_order = int(amount)
        except ValueError:
            await interaction.followup.send("Please provide a number or 'max'.", ephemeral=True)
            return
        if to_order <= 0:
            await interaction.followup.send("Amount must be at least 1.", ephemeral=True)
            return
        if to_order > balance:
            await interaction.followup.send(
                f"You only have **{balance}** JoinCoins.", ephemeral=True,
            )
            return

    order_id = await create_order(
        guild_id=interaction.guild.id,
        owner_id=interaction.user.id,
        members=to_order,
        coins=to_order,
        now=int(time.time()),
    )

    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1
        """, interaction.user.id, to_order)

    async with _pool.acquire() as conn:
        candidates = await conn.fetch("""
            SELECT user_id, access_token FROM joindev.users
            WHERE do_not_join = FALSE
            LIMIT $1
        """, to_order)

    added = 0
    for c in candidates:
        try:
            await interaction.guild.add_member(
                discord.Object(id=c["user_id"]),
                reason=f"JoinDev order #{order_id}",
            )
            await create_active_join(
                user_id=c["user_id"],
                guild_id=interaction.guild.id,
                order_id=order_id,
                now=int(time.time()),
            )
            added += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"Failed to add user {c['user_id']} to {interaction.guild.id}: {e}")

    await interaction.followup.send(
        f"✅ **Order #{order_id} placed!**\n"
        f"Members requested: **{to_order}**\n"
        f"Members added so far: **{added}**\n"
        f"Coins spent: **{to_order}**\n\n"
        f"Each member must stay **3 full days** for the order to count.\n"
        f"When all members complete their stay, the bot will leave your server.",
        ephemeral=True,
    )
