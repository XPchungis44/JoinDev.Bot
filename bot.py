"""
bot.py — JoinDev Discord bot
"""

import os
import time
import asyncio
import logging

import aiohttp
import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from welcome import build_welcome_embed
from database import (
    init_pool,
    get_pool,
    get_user,
    claim_daily,
    set_do_not_join,
    set_banned,
    set_appealed,
    set_farming_pool,
    add_server,
    get_active_servers,
    get_farming_pool_users,
    get_duplicate_ips,
    get_users_by_ip,
    mark_trusted,
    strip_coins,
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
    get_pending_welcome_dms,
    mark_welcome_sent,
    touch_auto_join,
    touch_buy_members,
    THREE_DAYS,
)

log = logging.getLogger("joindev.bot")

intents = discord.Intents.default()
intents.members = True
intents.dm_messages = True
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)

INSTALL_URL = (
    "https://discord.com/oauth2/authorize"
    "?client_id=1552458677821382656"
    "&scope=bot+applications.commands"
    "&permissions=8"
)

SUPPORT_SERVER_ID = 1512995317430096063
ADMIN_USER_ID = 1459373221756538923
TICKET_CHANNEL_URL = (
    "https://canary.discord.com/channels/"
    "1512995317430096063/1521312991507775599/1536401203997442140"
)

# Users who never get flagged as alts (your test accounts)
WHITELISTED_IDS = {
    1459373221756538923,  # your main
    # Add your test alt IDs here
}

COOLDOWN_AUTO_JOIN = 10
COOLDOWN_BUY_MEMBERS = 30
MAX_COINS = 10000
MAX_ORDER_SIZE = 50
NO_ORDER_TIMEOUT = 24 * 60 * 60
TRUST_PERIOD = 14 * 24 * 60 * 60  # 14 days


# ---------------------------------------------------------------
# BLACKLIST EMBED + DM
# ---------------------------------------------------------------
def build_blacklist_embed(user_id: int) -> discord.Embed:
    embed = discord.Embed(
        title="🚫 You Have Been Blacklisted",
        description=(
            "Your account has been flagged for **alting** — creating multiple accounts "
            "to exploit JoinCoins.\n\n"
            "All JoinCoins on this account have been removed."
        ),
        color=0xED4245,
    )
    embed.add_field(
        name="📩 How to Appeal",
        value=(
            f"**Preferred:** [Open a ticket here]({TICKET_CHANNEL_URL})\n\n"
            f"**Or** use this command (you **only get one** appeal):\n"
            f"`.appeal <your appeal note> {user_id}`\n\n"
            f"_To get your user ID: enable Developer Mode in Discord settings, "
            f"then right-click your profile and select 'Copy User ID'._"
        ),
        inline=False,
    )
    embed.set_footer(text="JoinDev • One appeal per account")
    return embed


async def _dm_blacklist(user_id: int):
    try:
        user = await bot.fetch_user(user_id)
        await user.send(embed=build_blacklist_embed(user_id))
        log.info(f"blacklist: DM sent to {user_id}")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"blacklist: could not DM {user_id}: {e}")


async def _notify_blacklist(user_id: int, interaction: discord.Interaction = None):
    """Sends the blacklist embed via interaction or DM."""
    if interaction:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=build_blacklist_embed(user_id), ephemeral=True)
            else:
                await interaction.response.send_message(embed=build_blacklist_embed(user_id), ephemeral=True)
            return
        except Exception:
            pass
    await _dm_blacklist(user_id)


# ---------------------------------------------------------------
# SHARED HELPER
# ---------------------------------------------------------------
async def _add_user_to_guild(session, guild_id, user_id, reason="JoinDev"):
    user_data = await get_user(user_id)
    if not user_data or not user_data.get("access_token"):
        log.error(f"add_user: no token for {user_id}")
        return False

    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {
        "Authorization": f"Bot {os.getenv('DISCORD_BOT_TOKEN')}",
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": reason[:512],
    }
    payload = {"access_token": user_data["access_token"]}

    try:
        async with session.put(url, headers=headers, json=payload) as resp:
            if resp.status in (201, 204):
                log.info(f"add_user: ✅ {user_id} → {guild_id}")
                return True
            body = await resp.text()
            log.error(f"add_user: ❌ {resp.status} for {user_id} → {guild_id}: {body}")
            return False
    except Exception as e:
        log.error(f"add_user: ❌ request failed for {user_id} → {guild_id}: {e}")
        return False


# ---------------------------------------------------------------
# SETUP HOOK
# ---------------------------------------------------------------
@bot.event
async def setup_hook():
    log.info("setup_hook: initializing DB pool...")
    await init_pool()
    log.info("setup_hook: DB pool ready.")


# ---------------------------------------------------------------
# ALT DETECTION TASK
# ---------------------------------------------------------------
@tasks.loop(hours=1)
async def detect_alts():
    """Finds shared IPs across accounts. Bans + strips coins."""
    log.info("detect_alts: scanning...")

    try:
        dupes = await get_duplicate_ips()
    except Exception as e:
        log.error(f"detect_alts: query failed: {e}")
        return

    if not dupes:
        log.info("detect_alts: no duplicates found")
        return

    for row in dupes:
        ip = row["ip_address"]
        try:
            users = await get_users_by_ip(ip)
        except Exception as e:
            log.error(f"detect_alts: fetch users for {ip} failed: {e}")
            continue

        # Filter out whitelisted + trusted + already banned
        flagged = [
            u for u in users
            if u["user_id"] not in WHITELISTED_IDS
            and not u.get("trusted")
            and not u.get("banned")
        ]

        # Need at least 2 non-whitelisted accounts sharing the IP
        if len(flagged) < 2:
            continue

        log.info(f"detect_alts: banning {len(flagged)} accounts on {ip}")

        for u in flagged:
            uid = u["user_id"]
            try:
                await set_banned(uid, True)
                await strip_coins(uid)
                await set_farming_pool(uid, False)
                log.info(f"detect_alts: banned + stripped {uid}")
                await _dm_blacklist(uid)
            except Exception as e:
                log.error(f"detect_alts: ban failed for {uid}: {e}")

    # Mark trusted users (2+ weeks, never flagged)
    now = int(time.time())
    try:
        async with get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE joindev.users
                SET trusted = TRUE
                WHERE trusted = FALSE
                  AND banned = FALSE
                  AND first_seen IS NOT NULL
                  AND first_seen <= $1
            """, now - TRUST_PERIOD)
    except Exception as e:
        log.error(f"detect_alts: trusted update failed: {e}")


# ---------------------------------------------------------------
# ON GUILD JOIN
# ---------------------------------------------------------------
@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info(f"on_guild_join: {guild.name} ({guild.id})")

    if guild.id == SUPPORT_SERVER_ID:
        return

    try:
        owner = await bot.fetch_user(guild.owner_id)
        embed = discord.Embed(
            title=f"👋 Thanks for adding me to {guild.name}!",
            description="A few things you need to know before you get started.",
            color=0x5865F2,
        )
        embed.add_field(
            name="1️⃣ 24-Hour Grace Period",
            value="I'll leave if no order is placed within **24 hours** — but you can always add me back!",
            inline=False,
        )
        embed.add_field(
            name="2️⃣ Refunds Are Automatic",
            value="If a user leaves before completing their stay, your JoinCoin is **refunded**.",
            inline=False,
        )
        embed.add_field(
            name="3️⃣ 3-Day Requirement",
            value="A user must stay in your server for **3 full days** to consume one of your JoinCoins.",
            inline=False,
        )
        embed.add_field(
            name="4️⃣ Cancel Anytime",
            value="`/cancel_order` — Refunds unfilled slots only\n`/full_cancel_order` — Completely undoes the order",
            inline=False,
        )
        embed.add_field(
            name="🆘 Need Help?",
            value=f"[Open a ticket here]({TICKET_CHANNEL_URL})",
            inline=False,
        )
        embed.set_footer(text="JoinDev • Server growth made easy!")
        await owner.send(embed=embed)
    except discord.Forbidden:
        log.warning(f"on_guild_join: cannot DM owner")
    except Exception as e:
        log.error(f"on_guild_join: DM failed: {e}")


# ---------------------------------------------------------------
# 24H AUTO-LEAVE
# ---------------------------------------------------------------
@tasks.loop(minutes=10)
async def leave_unused_guilds():
    now = int(time.time())

    for guild in bot.guilds:
        if guild.id == SUPPORT_SERVER_ID:
            continue

        try:
            async with get_pool().acquire() as conn:
                order_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM joindev.orders WHERE guild_id = $1",
                    guild.id,
                )
        except Exception as e:
            log.error(f"leave_unused: DB error for {guild.id}: {e}")
            continue

        if order_count > 0:
            continue

        me = guild.me
        if me is None or me.joined_at is None:
            continue

        joined_ts = int(me.joined_at.timestamp())
        if now - joined_ts < NO_ORDER_TIMEOUT:
            continue

        try:
            await guild.leave()
            log.info(f"leave_unused: left {guild.name}")
        except discord.HTTPException as e:
            log.error(f"leave_unused: failed: {e}")


# ---------------------------------------------------------------
# WELCOME QUEUE
# ---------------------------------------------------------------
@tasks.loop(seconds=3)
async def watch_welcome_queue():
    try:
        pending = await get_pending_welcome_dms()
    except Exception as e:
        log.error(f"watch_welcome_queue: fetch failed — {e}")
        return

    if not pending:
        return

    for row in pending:
        user_id = row["user_id"]

        async with aiohttp.ClientSession() as session:
            await _add_user_to_guild(session, SUPPORT_SERVER_ID, user_id, "JoinDev authorization")

        try:
            user = await bot.fetch_user(user_id)
            await user.send(embed=build_welcome_embed())
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"watch_welcome_queue: DM failed for {user_id}: {e}")

        await mark_welcome_sent(row["id"])


# ---------------------------------------------------------------
# 3-DAY REWARD CHECKER
# ---------------------------------------------------------------
@tasks.loop(hours=1)
async def check_join_rewards():
    pending = await get_pending_joins()
    if not pending:
        return

    for join in pending:
        user_id = join["user_id"]
        guild_id = join["guild_id"]
        order_id = join["order_id"]

        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                await mark_join_left_early(join["id"])
                if order_id:
                    await _refund_order_slot(order_id, user_id)
                continue

            member = guild.get_member(user_id)
            if member is None:
                await mark_join_left_early(join["id"])
                if order_id:
                    await _refund_order_slot(order_id, user_id)
                continue

            await mark_join_rewarded(join["id"])
            new_balance = await add_coins(user_id, 1)

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
                    try:
                        owner = await bot.fetch_user(updated["owner_id"])
                        await owner.send(
                            f"✅ **Your order is complete!**\n"
                            f"All **{updated['members_requested']}** members completed their stay."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                    await _leave_guild(guild_id)

        except Exception as e:
            log.error(f"check_join_rewards: error on {join['id']}: {e}")


async def _refund_order_slot(order_id: int, user_id: int):
    try:
        order = await get_order(order_id)
        if not order or order["status"] != "active":
            return
        await add_coins(order["owner_id"], 1)
        try:
            owner = await bot.fetch_user(order["owner_id"])
            await owner.send(
                f"💰 **Refund** — A user left your server early. **+1 JoinCoin** refunded."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    except Exception as e:
        log.error(f"_refund_order_slot: failed: {e}")


async def _leave_guild(guild_id):
    if guild_id == SUPPORT_SERVER_ID:
        return
    guild = bot.get_guild(guild_id)
    if guild:
        try:
            await guild.leave()
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------
# TOKEN REFRESH
# ---------------------------------------------------------------
@tasks.loop(hours=12)
async def refresh_tokens():
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    now = int(time.time())

    for u in await get_all_users():
        if not u["token_expires_at"] or u["token_expires_at"] - now > 86400:
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
                u["user_id"], new["access_token"], new["refresh_token"],
                int(time.time()) + new.get("expires_in", 604800),
            )
        except Exception as e:
            log.error(f"refresh_tokens: failed {u['user_id']}: {e}")


# ---------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------
@bot.event
async def on_ready():
    log.info(f"on_ready: {bot.user} (ID: {bot.user.id})")

    try:
        synced = await bot.tree.sync()
        log.info(f"on_ready: synced {len(synced)} commands")
    except Exception as e:
        log.error(f"on_ready: sync failed: {e}")

    if not watch_welcome_queue.is_running():
        watch_welcome_queue.start()
    if not check_join_rewards.is_running():
        check_join_rewards.start()
    if not refresh_tokens.is_running():
        refresh_tokens.start()
    if not leave_unused_guilds.is_running():
        leave_unused_guilds.start()
    if not detect_alts.is_running():
        detect_alts.start()


# ---------------------------------------------------------------
# BANNED CHECK DECORATOR
# ---------------------------------------------------------------
async def check_banned(interaction: discord.Interaction) -> bool:
    """Returns True if the user is banned. Sends the blacklist embed."""
    user = await get_user(interaction.user.id)
    if user and user.get("banned"):
        await _notify_blacklist(interaction.user.id, interaction)
        return True
    return False


# ---------------------------------------------------------------
# HELP
# ---------------------------------------------------------------
@bot.tree.command(name="help", description="Show all JoinDev commands.")
async def help_cmd(interaction: discord.Interaction):
    if await check_banned(interaction):
        return

    embed = discord.Embed(title="📖 JoinDev Commands", color=0x5865F2)
    embed.add_field(
        name="🪙 Earn JoinCoins",
        value="`/daily` — Claim coins\n`/balance` — Check balance\n`/status` — Your status",
        inline=False,
    )
    embed.add_field(
        name="🚀 Farm Servers",
        value="`/auto_join` — Join the farming pool\n`/cancel_joinr` — Leave the pool\n`/resume_joinr` — Rejoin the pool",
        inline=False,
    )
    embed.add_field(
        name="👥 Grow Your Server",
        value="`/submit_server` — Add your server\n`/buy_members` — Get members\n`/cancel_order` / `/full_cancel_order`",
        inline=False,
    )
    embed.set_footer(text="JoinDev • Beta")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------
# USER COMMANDS
# ---------------------------------------------------------------
@bot.tree.command(name="ping", description="Check if JoinDev is online.")
async def ping(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    await interaction.response.send_message(f"🏓 Pong! **{round(bot.latency * 1000)}ms**", ephemeral=True)


@bot.tree.command(name="balance", description="Check your JoinCoin balance.")
async def balance(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first: https://xpchungis44.github.io/JoinDev/", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🪙 **{user['joindev_coins']} JoinCoins**\n🔥 Streak: **{user['daily_streak']}**",
        ephemeral=True,
    )


@bot.tree.command(name="status", description="See your active joins.")
async def status_cmd(interaction: discord.Interaction):
    if await check_banned(interaction):
        return

    from database import get_user_active_joins
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first.", ephemeral=True)
        return

    joins = await get_user_active_joins(interaction.user.id)
    embed = discord.Embed(title="📊 Your JoinDev Status", color=0x5865F2)
    embed.add_field(name="🪙 Coins", value=f"**{user['joindev_coins']}**", inline=True)
    embed.add_field(name="🔥 Streak", value=f"**{user['daily_streak']}**", inline=True)
    embed.add_field(name="🎯 In Pool", value="✅" if user.get("farming_pool") else "❌", inline=True)
    embed.add_field(name="🔗 Active Joins", value=f"**{len(joins)}**", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="daily", description="Claim your daily JoinCoins reward.")
async def daily(interaction: discord.Interaction):
    if await check_banned(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send("Authorize first.", ephemeral=True)
        return

    result = await claim_daily(interaction.user.id, int(time.time()))
    if not result["success"]:
        if result.get("reason") == "cooldown":
            s = result["seconds_left"]
            await interaction.followup.send(f"⏳ Come back in **{s // 3600}h {(s % 3600) // 60}m**.", ephemeral=True)
        else:
            await interaction.followup.send("Something went wrong.", ephemeral=True)
        return

    embed = discord.Embed(title="🪙 Daily Reward Claimed!", color=0x5865F2)
    embed.add_field(name="Coins", value=f"**+{result['coins_awarded']}**", inline=True)
    embed.add_field(name="Streak", value=f"🔥 **{result['new_streak']}**", inline=True)
    embed.add_field(name="Balance", value=f"**{result['new_balance']}**", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel_joinr", description="Leave the farming pool.")
async def cancel_joinr(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first.", ephemeral=True)
        return
    if user["do_not_join"]:
        await interaction.response.send_message("🚫 Already disabled.", ephemeral=True)
        return
    await set_do_not_join(interaction.user.id, True)
    await set_farming_pool(interaction.user.id, False)
    await interaction.response.send_message("✅ Removed from the farming pool.", ephemeral=True)


@bot.tree.command(name="resume_joinr", description="Rejoin the farming pool.")
async def resume_joinr(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first.", ephemeral=True)
        return
    if not user["do_not_join"]:
        await interaction.response.send_message("✅ Already in the pool.", ephemeral=True)
        return
    await set_do_not_join(interaction.user.id, False)
    await set_farming_pool(interaction.user.id, True)
    await interaction.response.send_message("✅ Rejoined the farming pool.", ephemeral=True)


@bot.tree.command(name="submit_server", description="Add your server to the JoinDev pool.")
@app_commands.describe(invite="A permanent invite link to your server")
async def submit_server(interaction: discord.Interaction, invite: str):
    if await check_banned(interaction):
        return

    if not interaction.guild:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the server owner can submit.", ephemeral=True)
        return
    if not invite.startswith(("https://discord.gg/", "https://discord.com/invite/")):
        await interaction.response.send_message("Invalid invite link.", ephemeral=True)
        return

    await add_server(interaction.guild.id, interaction.user.id, invite, int(time.time()))

    embed = discord.Embed(
        title="✅ Server Added to Pool!",
        description=f"**Add me to that server → [Install Link]({INSTALL_URL})**\n\nThen your order will be complete!",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="auto_join", description="Join the farming pool.")
@app_commands.describe(amount="Optional: how many coins to spend now, or 'max'")
async def auto_join(interaction: discord.Interaction, amount: str = "0"):
    if await check_banned(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send("Authorize first.", ephemeral=True)
        return

    await set_farming_pool(interaction.user.id, True)

    now = int(time.time())
    last = user.get("last_auto_join") or 0
    if now - last < COOLDOWN_AUTO_JOIN:
        wait = COOLDOWN_AUTO_JOIN - (now - last)
        await interaction.followup.send(f"✅ You're in the farming pool!\n⏳ Wait **{wait}s** before joining now.", ephemeral=True)
        return

    await touch_auto_join(interaction.user.id, now)

    try:
        if amount.lower() == "max":
            spend = user["joindev_coins"]
        elif amount in ("0", ""):
            spend = 0
        else:
            spend = int(amount)
    except (ValueError, AttributeError):
        spend = 0

    if spend <= 0:
        await interaction.followup.send(
            "✅ **You're in the farming pool!**\n\nYou'll be added to servers as they become available.",
            ephemeral=True,
        )
        return

    balance = user["joindev_coins"]
    if spend > balance:
        spend = balance
    if spend <= 0:
        await interaction.followup.send("🪙 No coins to spend.", ephemeral=True)
        return

    servers = await get_active_servers()
    if not servers:
        await interaction.followup.send("✅ In the pool! No servers available right now.", ephemeral=True)
        return

    to_join = min(spend, len(servers))
    servers = servers[:to_join]

    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1",
            interaction.user.id, to_join,
        )

    async with aiohttp.ClientSession() as session:
        tasks = [_add_user_to_guild(session, srv["guild_id"], interaction.user.id, "JoinDev auto-join") for srv in servers]
        results = await asyncio.gather(*tasks)

    joined = sum(1 for r in results if r)
    failed = len(results) - joined

    for srv, ok in zip(servers, results):
        if ok:
            await create_active_join(interaction.user.id, srv["guild_id"], None, now)

    if failed:
        await add_coins(interaction.user.id, failed)

    await interaction.followup.send(
        f"✅ **In the pool!**\nJoined **{joined}** now. Failed: **{failed}** (refunded).",
        ephemeral=True,
    )


@bot.tree.command(name="buy_members", description="Spend JoinCoins to bring members to your server.")
@app_commands.describe(amount="How many members you want, or 'max'")
async def buy_members(interaction: discord.Interaction, amount: str):
    if await check_banned(interaction):
        return

    await interaction.response.send_message(
        "⏳ **Adding users — this may take a while.**\nCancel with `/cancel_order`.",
        ephemeral=True,
    )

    if not interaction.guild:
        await interaction.edit_original_response(content="Run this in a server.")
        return
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.edit_original_response(content="Only the server owner can order.")
        return

    user = await get_user(interaction.user.id)
    if not user:
        await interaction.edit_original_response(content="Authorize first.")
        return

    now = int(time.time())
    last = user.get("last_buy_members") or 0
    if now - last < COOLDOWN_BUY_MEMBERS:
        wait = COOLDOWN_BUY_MEMBERS - (now - last)
        await interaction.edit_original_response(content=f"⏳ Wait **{wait}s**.")
        return

    balance = user["joindev_coins"]
    if balance <= 0:
        await interaction.edit_original_response(content="🪙 No coins.")
        return

    if amount.lower() == "max":
        to_order = balance
    else:
        try:
            to_order = int(amount)
        except ValueError:
            await interaction.edit_original_response(content="Provide a number or 'max'.")
            return
        if to_order <= 0 or to_order > balance:
            await interaction.edit_original_response(content=f"Invalid. You have {balance}.")
            return

    to_order = min(to_order, MAX_ORDER_SIZE)

    coins_deducted = False
    order_id = None

    try:
        candidates = await get_farming_pool_users(limit=to_order * 2, exclude_user_ids=[interaction.user.id])
        if not candidates:
            await interaction.edit_original_response(content="❌ No eligible users in the pool.")
            return

        targets = [c["user_id"] for c in candidates if not interaction.guild.get_member(c["user_id"])]
        if not targets:
            await interaction.edit_original_response(content="❌ All eligible users already in your server.")
            return

        targets = targets[:to_order]

        order_id = await create_order(interaction.guild.id, interaction.user.id, len(targets), len(targets), now)
        await touch_buy_members(interaction.user.id, now)

        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1",
                interaction.user.id, len(targets),
            )
        coins_deducted = True

        async with aiohttp.ClientSession() as session:
            tasks = [_add_user_to_guild(session, interaction.guild.id, uid, f"Order #{order_id}") for uid in targets]
            results = await asyncio.gather(*tasks)

        added = 0
        for uid, ok in zip(targets, results):
            if ok:
                await create_active_join(uid, interaction.guild.id, order_id, now)
                added += 1

        refund = len(targets) - added
        if refund > 0:
            await add_coins(interaction.user.id, refund)

        await interaction.edit_original_response(
            content=(
                f"✅ **Order #{order_id} placed!**\n"
                f"Added: **{added}** • Refunded: **{refund}**"
            )
        )
    except Exception as e:
        log.error(f"buy_members: {e}")
        if coins_deducted:
            await add_coins(interaction.user.id, to_order)
        await interaction.edit_original_response(content=f"❌ Error. **{to_order} coins refunded.**")


# ---------------------------------------------------------------
# CANCEL COMMANDS
# ---------------------------------------------------------------
@bot.tree.command(name="cancel_order", description="Refund unfilled slots.")
async def cancel_order(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.followup.send("Only the server owner can cancel.", ephemeral=True)
        return

    async with get_pool().acquire() as conn:
        order = await conn.fetchrow("""
            SELECT * FROM joindev.orders WHERE guild_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """, interaction.guild.id)

    if not order:
        await interaction.followup.send("❌ No active order.", ephemeral=True)
        return

    remaining = order["members_requested"] - order["members_completed"]
    if remaining > 0:
        await add_coins(interaction.user.id, remaining)

    async with get_pool().acquire() as conn:
        await conn.execute("""
            UPDATE joindev.orders SET status = 'cancelled', completed_at = $2 WHERE order_id = $1
        """, order["order_id"], int(time.time()))

    await interaction.followup.send(f"✅ Cancelled. Refunded **{remaining}**. Members stay.", ephemeral=True)
    await _leave_guild(interaction.guild.id)


@bot.tree.command(name="full_cancel_order", description="Fully undo the order.")
async def full_cancel_order(interaction: discord.Interaction):
    if await check_banned(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.followup.send("Only the server owner can cancel.", ephemeral=True)
        return

    async with get_pool().acquire() as conn:
        order = await conn.fetchrow("""
            SELECT * FROM joindev.orders WHERE guild_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """, interaction.guild.id)

    if not order:
        await interaction.followup.send("❌ No active order.", ephemeral=True)
        return

    order_id = order["order_id"]
    remaining = order["members_requested"] - order["members_completed"]
    if remaining > 0:
        await add_coins(interaction.user.id, remaining)

    async with get_pool().acquire() as conn:
        joins = await conn.fetch("""
            SELECT * FROM joindev.active_joins
            WHERE order_id = $1 AND rewarded = FALSE AND left_early = FALSE
        """, order_id)

    kicked = 0
    for join in joins:
        uid = join["user_id"]
        try:
            user = await bot.fetch_user(uid)
            try:
                await user.send(
                    f"📢 **Order Cancelled**\n\nWe're sorry, but the server you joined has fully cancelled. "
                    f"🪙 **You still get the JoinCoin.**"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

            await add_coins(uid, 1)

            member = interaction.guild.get_member(uid)
            if member:
                try:
                    await member.kick(reason=f"Order #{order_id} cancelled")
                    kicked += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            log.error(f"full_cancel: {uid}: {e}")

    async with get_pool().acquire() as conn:
        await conn.execute("""
            UPDATE joindev.active_joins SET rewarded = TRUE, left_early = TRUE, checked_at = $2
            WHERE order_id = $1 AND rewarded = FALSE
        """, order_id, int(time.time()))
        await conn.execute("""
            UPDATE joindev.orders SET status = 'cancelled', completed_at = $2 WHERE order_id = $1
        """, order_id, int(time.time()))

    await interaction.followup.send(f"✅ Fully cancelled. Kicked **{kicked}**, refunded **{remaining}**.", ephemeral=True)
    await _leave_guild(interaction.guild.id)


# ---------------------------------------------------------------
# APPEAL COMMAND (prefix, available to banned users)
# ---------------------------------------------------------------
@bot.command(name="appeal")
async def appeal(ctx: commands.Context, note: str, user_id: int):
    """Available even to banned users. Usage: .appeal <note> <your_user_id>"""
    # Must match author's ID exactly (proof they know it)
    if ctx.author.id != user_id:
        try:
            await ctx.send("❌ The user ID must match your own. Enable Developer Mode and copy your ID.", delete_after=10)
        except Exception:
            pass
        return

    user = await get_user(user_id)
    if not user:
        await ctx.send("❌ You're not registered with JoinDev.", delete_after=10)
        return

    if not user.get("banned"):
        await ctx.send("✅ You're not banned — no appeal needed.", delete_after=10)
        return

    if user.get("appealed"):
        await ctx.send("🚫 You have already used your one appeal.", delete_after=10)
        return

    await set_appealed(user_id, note)

    # Notify admin
    try:
        admin = await bot.fetch_user(ADMIN_USER_ID)
        embed = discord.Embed(
            title="📩 New Appeal",
            description=f"User <@{user_id}> (`{user_id}`) has appealed.",
            color=0xF0B232,
        )
        embed.add_field(name="Appeal Note", value=note[:1024], inline=False)
        embed.add_field(
            name="Actions",
            value=f"`.unban {user_id}` — Accept\n`.check` — View stats",
            inline=False,
        )
        await admin.send(embed=embed)
    except Exception as e:
        log.error(f"appeal: notify admin failed: {e}")

    await ctx.send(
        "✅ **Your appeal has been submitted.**\n"
        "The team will review it and you'll be notified via DM.\n"
        "_You only get one appeal._"
    )


# ---------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID


@bot.command(name="unban")
async def admin_unban(ctx: commands.Context, user_id: int):
    if not is_admin(ctx.author.id):
        return

    user = await get_user(user_id)
    if not user:
        await ctx.send(f"❌ `{user_id}` not registered.")
        return

    await set_banned(user_id, False)

    try:
        target = await bot.fetch_user(user_id)
        await target.send(
            "✅ **Your appeal has been accepted.**\n\n"
            "You've been unbanned from JoinDev. Please follow the rules.\n\n"
            "⚠️ **Next warning = unappealable ban.**"
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    await ctx.send(f"✅ Unbanned `{user_id}` and sent notification.")


@bot.command(name="ban")
async def admin_ban(ctx: commands.Context, user_id: int):
    if not is_admin(ctx.author.id):
        return
    await set_banned(user_id, True)
    await strip_coins(user_id)
    await set_farming_pool(user_id, False)
    await _dm_blacklist(user_id)
    await ctx.send(f"✅ Banned + stripped `{user_id}`.")


@bot.command(name="give")
async def admin_give(ctx: commands.Context, user_id: int, amount: int):
    if not is_admin(ctx.author.id):
        return
    user = await get_user(user_id)
    if not user:
        await ctx.send(f"❌ `{user_id}` not registered.")
        return
    new_balance = await add_coins(user_id, amount)
    await ctx.send(f"✅ Gave **{amount}** to <@{user_id}>. Balance: **{new_balance}**")


@bot.command(name="join")
async def admin_join(ctx: commands.Context, guild_id: int, amount: str):
    if not is_admin(ctx.author.id):
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        await ctx.send(f"❌ Not in `{guild_id}`.")
        return

    try:
        n = 1000 if amount.lower() == "max" else int(amount)
    except ValueError:
        await ctx.send("❌ Number or 'max'.")
        return

    candidates = await get_farming_pool_users(limit=n * 2)
    targets = [c["user_id"] for c in candidates if not guild.get_member(c["user_id"])]
    if not targets:
        await ctx.send("No eligible users.")
        return

    targets = targets[:n]

    async with aiohttp.ClientSession() as session:
        tasks = [_add_user_to_guild(session, guild_id, uid, "Admin .join") for uid in targets]
        results = await asyncio.gather(*tasks)

    added = sum(1 for r in results if r)
    await ctx.send(f"✅ Added **{added}** to **{guild.name}**.")


@bot.command(name="check")
async def admin_check(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return

    try:
        async with get_pool().acquire() as conn:
            auth_count = await conn.fetchval("SELECT COUNT(*) FROM joindev.users")
            pool_count = await conn.fetchval("SELECT COUNT(*) FROM joindev.users WHERE farming_pool = TRUE")
            banned_count = await conn.fetchval("SELECT COUNT(*) FROM joindev.users WHERE banned = TRUE")
            trusted_count = await conn.fetchval("SELECT COUNT(*) FROM joindev.users WHERE trusted = TRUE")
            active_orders = await conn.fetchval("SELECT COUNT(*) FROM joindev.orders WHERE status = 'active'")
            active_joins = await conn.fetchval("SELECT COUNT(*) FROM joindev.active_joins WHERE rewarded = FALSE AND left_early = FALSE")
            total_coins = await conn.fetchval("SELECT COALESCE(SUM(joindev_coins), 0) FROM joindev.users")
    except Exception as e:
        await ctx.send(f"❌ DB error: `{e}`")
        return

    embed = discord.Embed(title="📊 JoinDev Stats", color=0x5865F2)
    embed.add_field(name="👥 Users", value=f"**{auth_count}**", inline=True)
    embed.add_field(name="🎯 In Pool", value=f"**{pool_count}**", inline=True)
    embed.add_field(name="✅ Trusted", value=f"**{trusted_count}**", inline=True)
    embed.add_field(name="🚫 Banned", value=f"**{banned_count}**", inline=True)
    embed.add_field(name="📦 Active Orders", value=f"**{active_orders}**", inline=True)
    embed.add_field(name="🔗 Pending Joins", value=f"**{active_joins}**", inline=True)
    embed.add_field(name="🪙 Coins", value=f"**{total_coins}**", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="leave")
async def admin_leave(ctx: commands.Context, guild_id: int):
    if not is_admin(ctx.author.id):
        return
    if guild_id == SUPPORT_SERVER_ID:
        await ctx.send("❌ Refusing to leave support server.")
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        await ctx.send(f"❌ Not in `{guild_id}`.")
        return
    await guild.leave()
    await ctx.send(f"✅ Left **{guild.name}**.")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_BOT_TOKEN"))
