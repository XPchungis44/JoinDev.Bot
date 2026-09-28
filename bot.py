"""
bot.py — JoinDev Discord bot (commands + background tasks)
"""

import os
import time
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
    get_pending_welcome_dms,
    mark_welcome_sent,
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


# ---------------------------------------------------------------
# SHARED HELPER — ADD A USER TO A GUILD VIA REST API
# ---------------------------------------------------------------
async def _add_user_to_guild(guild_id: int, user_id: int, reason: str = "JoinDev") -> bool:
    """
    Adds a user to a guild using their stored OAuth token.
    Returns True on success (or already-a-member), False otherwise.
    """
    user_data = await get_user(user_id)
    if not user_data or not user_data.get("access_token"):
        log.error(f"add_user: no OAuth token for user {user_id}")
        return False

    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {
        "Authorization": f"Bot {os.getenv('DISCORD_BOT_TOKEN')}",
        "Content-Type": "application/json",
        "X-Audit-Log-Reason": reason[:512],
    }
    payload = {"access_token": user_data["access_token"]}

    try:
        async with aiohttp.ClientSession() as session:
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
# WELCOME DM QUEUE WATCHER
# ---------------------------------------------------------------
@tasks.loop(seconds=5)
async def watch_welcome_queue():
    log.info("watch_welcome_queue: tick")
    try:
        pending = await get_pending_welcome_dms()
    except Exception as e:
        log.error(f"watch_welcome_queue: fetch failed — {type(e).__name__}: {e}")
        return

    log.info(f"watch_welcome_queue: {len(pending)} pending")

    if not pending:
        return

    for row in pending:
        user_id = row["user_id"]
        log.info(f"watch_welcome_queue: processing user {user_id} (row id {row['id']})")

        # 1. Add to support server FIRST
        await _add_to_support_server(user_id)

        # 2. Then send the DM
        try:
            user = await bot.fetch_user(user_id)
            await user.send(embed=build_welcome_embed())
            log.info(f"watch_welcome_queue: ✅ DM sent to {user_id}")
        except discord.Forbidden:
            log.warning(f"watch_welcome_queue: ❌ Cannot DM {user_id} — DMs closed.")
        except discord.HTTPException as e:
            log.error(f"watch_welcome_queue: ❌ DM failed for {user_id}: {e}")

        # 3. Mark as done
        try:
            await mark_welcome_sent(row["id"])
            log.info(f"watch_welcome_queue: marked row {row['id']} as sent")
        except Exception as e:
            log.error(f"watch_welcome_queue: failed to mark row {row['id']}: {e}")


async def _add_to_support_server(user_id: int):
    """Adds a user to the support server using their OAuth2 token via REST."""
    guild = bot.get_guild(SUPPORT_SERVER_ID)
    if not guild:
        log.warning(f"support_server: bot NOT in guild {SUPPORT_SERVER_ID}")
        return

    if guild.get_member(user_id) is not None:
        log.info(f"support_server: user {user_id} already in guild")
        return

    ok = await _add_user_to_guild(SUPPORT_SERVER_ID, user_id, reason="JoinDev authorization")
    if not ok:
        log.error(f"support_server: failed to add {user_id}")


# ---------------------------------------------------------------
# 3-DAY JOIN REWARD CHECKER
# ---------------------------------------------------------------
@tasks.loop(hours=1)
async def check_join_rewards():
    pending = await get_pending_joins()
    if not pending:
        return

    log.info(f"check_join_rewards: {len(pending)} pending joins")

    for join in pending:
        user_id = join["user_id"]
        guild_id = join["guild_id"]
        order_id = join["order_id"]

        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                await mark_join_left_early(join["id"])
                continue

            member = guild.get_member(user_id)
            if member is None:
                await mark_join_left_early(join["id"])
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
                            f"All **{updated['members_requested']}** members completed their stay.\n"
                            f"The bot will now leave your server."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                    await _leave_guild(guild_id)

        except Exception as e:
            log.error(f"check_join_rewards: error on join {join['id']}: {e}")


async def _leave_guild(guild_id: int):
    if guild_id == SUPPORT_SERVER_ID:
        log.warning(f"leave_guild: refusing to leave support server")
        return
    guild = bot.get_guild(guild_id)
    if guild:
        try:
            await guild.leave()
            log.info(f"leave_guild: left {guild_id}")
        except discord.HTTPException as e:
            log.error(f"leave_guild: failed — {e}")


# ---------------------------------------------------------------
# TOKEN REFRESH
# ---------------------------------------------------------------
@tasks.loop(hours=12)
async def refresh_tokens():
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    now = int(time.time())

    for u in await get_all_users():
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
                u["user_id"], new["access_token"], new["refresh_token"],
                int(time.time()) + new.get("expires_in", 604800),
            )
            log.info(f"refresh_tokens: refreshed {u['user_id']}")
        except Exception as e:
            log.error(f"refresh_tokens: failed for {u['user_id']}: {e}")


# ---------------------------------------------------------------
# EVENTS
# ---------------------------------------------------------------
@bot.event
async def on_ready():
    log.info(f"on_ready: logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"on_ready: in {len(bot.guilds)} guild(s)")
    for g in bot.guilds:
        log.info(f"on_ready: guild → {g.name} ({g.id})")

    try:
        synced = await bot.tree.sync()
        log.info(f"on_ready: synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"on_ready: slash sync failed: {e}")

    if not watch_welcome_queue.is_running():
        watch_welcome_queue.start()
        log.info("on_ready: started watch_welcome_queue")
    else:
        log.info("on_ready: watch_welcome_queue already running")

    if not check_join_rewards.is_running():
        check_join_rewards.start()
        log.info("on_ready: started check_join_rewards")

    if not refresh_tokens.is_running():
        refresh_tokens.start()
        log.info("on_ready: started refresh_tokens")


# ---------------------------------------------------------------
# HELP
# ---------------------------------------------------------------
@bot.tree.command(name="help", description="Show all JoinDev commands.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 JoinDev Commands",
        description="*Server growth made easy!*",
        color=0x5865F2,
    )
    embed.add_field(
        name="🪙 Earn JoinCoins",
        value="`/daily` — Claim 3+ JoinCoins every 24 hours\n`/balance` — Check your balance",
        inline=False,
    )
    embed.add_field(
        name="🚀 Farm Servers",
        value="`/auto_join <amount|max>` — Spend coins to join servers\n`/cancel_joinr` — Stop auto-joins\n`/resume_joinr` — Re-enable auto-joins",
        inline=False,
    )
    embed.add_field(
        name="👥 Grow Your Server",
        value="`/submit_server <invite>` — Add your server\n`/buy_members <amount|max>` — Get members",
        inline=False,
    )
    embed.add_field(
        name="🔗 Links",
        value="Website: https://xpchungis44.github.io/JoinDev/\nSupport: https://discord.gg/HJbbKYbr2y",
        inline=False,
    )
    embed.set_footer(text="JoinDev • Beta • Server growth made easy!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------
# USER SLASH COMMANDS
# ---------------------------------------------------------------
@bot.tree.command(name="ping", description="Check if JoinDev is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: **{round(bot.latency * 1000)}ms**", ephemeral=True,
    )


@bot.tree.command(name="balance", description="Check your JoinCoin balance.")
async def balance(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "Authorize first: https://xpchungis44.github.io/JoinDev/", ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"🪙 **{user['joindev_coins']} JoinCoins**\n"
        f"🔥 Daily streak: **{user['daily_streak']}**",
        ephemeral=True,
    )


@bot.tree.command(name="daily", description="Claim your daily JoinCoins reward.")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send(
            "Authorize first: https://xpchungis44.github.io/JoinDev/", ephemeral=True,
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
    embed.add_field(name="Coins", value=f"**+{result['coins_awarded']}**", inline=True)
    embed.add_field(name="Streak", value=f"🔥 **{result['new_streak']}**", inline=True)
    embed.add_field(name="Balance", value=f"**{result['new_balance']}**", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel_joinr", description="Stop all pending auto-joins.")
async def cancel_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first.", ephemeral=True)
        return
    if user["do_not_join"]:
        await interaction.response.send_message("🚫 Already disabled.", ephemeral=True)
        return
    await set_do_not_join(interaction.user.id, True)
    await interaction.response.send_message("✅ Auto-joins disabled.", ephemeral=True)


@bot.tree.command(name="resume_joinr", description="Re-enable auto-joins.")
async def resume_joinr(interaction: discord.Interaction):
    user = await get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("Authorize first.", ephemeral=True)
        return
    if not user["do_not_join"]:
        await interaction.response.send_message("✅ Already active.", ephemeral=True)
        return
    await set_do_not_join(interaction.user.id, False)
    await interaction.response.send_message("✅ Auto-joins re-enabled.", ephemeral=True)


@bot.tree.command(name="submit_server", description="Add your server to the JoinDev pool.")
@app_commands.describe(invite="A permanent invite link to your server")
async def submit_server(interaction: discord.Interaction, invite: str):
    if not interaction.guild:
        await interaction.response.send_message("Run this inside your server.", ephemeral=True)
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
        description=(
            f"**Add me to that server by clicking here → [Install Link]({INSTALL_URL})**\n\n"
            "Then your order will be complete!"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="What happens next?",
        value="Run `/buy_members` to place an order. Members stay 3 days, then I leave automatically.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="auto_join", description="Spend JoinCoins to farm servers.")
@app_commands.describe(amount="How many JoinCoins to spend, or 'max'")
async def auto_join(interaction: discord.Interaction, amount: str):
    await interaction.response.defer(ephemeral=True)

    user = await get_user(interaction.user.id)
    if not user:
        await interaction.followup.send("Authorize first.", ephemeral=True)
        return
    if user["do_not_join"]:
        await interaction.followup.send("🚫 Auto-joins disabled.", ephemeral=True)
        return

    balance = user["joindev_coins"]
    if balance <= 0:
        await interaction.followup.send("🪙 No coins. Run `/daily`.", ephemeral=True)
        return

    if amount.lower() == "max":
        to_join = balance
    else:
        try:
            to_join = int(amount)
        except ValueError:
            await interaction.followup.send("Provide a number or 'max'.", ephemeral=True)
            return
        if to_join <= 0 or to_join > balance:
            await interaction.followup.send("Invalid amount.", ephemeral=True)
            return

    servers = await get_active_servers()
    if not servers:
        await interaction.followup.send("📭 No servers in the pool.", ephemeral=True)
        return

    to_join = min(to_join, len(servers))
    servers = servers[:to_join]

    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1",
            interaction.user.id, to_join,
        )

    joined = 0
    failed = 0
    for srv in servers:
        guild = bot.get_guild(srv["guild_id"])
        if not guild:
            failed += 1
            continue

        ok = await _add_user_to_guild(
            srv["guild_id"], interaction.user.id, reason="JoinDev auto-join"
        )
        if ok:
            await create_active_join(
                interaction.user.id, srv["guild_id"], None, int(time.time())
            )
            joined += 1
        else:
            failed += 1

    if failed:
        await add_coins(interaction.user.id, failed)

    await interaction.followup.send(
        f"✅ Joined **{joined}** server(s).\n"
        f"Coins spent: **{joined}** • Failed: **{failed}** (refunded)",
        ephemeral=True,
    )


@bot.tree.command(name="buy_members", description="Spend JoinCoins to bring members to your server.")
@app_commands.describe(amount="How many members you want, or 'max'")
async def buy_members(interaction: discord.Interaction, amount: str):
    await interaction.response.send_message(
        "⏳ **Adding users to your server — this may take a while.**\n"
        "You can cancel at any time with `/cancel_joinr`.",
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
        await interaction.edit_original_response(
            content="Authorize first: https://xpchungis44.github.io/JoinDev/"
        )
        return

    balance = user["joindev_coins"]
    if balance <= 0:
        await interaction.edit_original_response(content="🪙 No coins. Run `/daily`.")
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

    order_id = await create_order(
        interaction.guild.id, interaction.user.id, to_order, to_order, int(time.time()),
    )

    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET joindev_coins = joindev_coins - $2 WHERE user_id = $1",
            interaction.user.id, to_order,
        )
        candidates = await conn.fetch("""
            SELECT user_id FROM joindev.users
            WHERE do_not_join = FALSE AND user_id != $1
            LIMIT $2
        """, interaction.user.id, to_order)

    added = 0
    for c in candidates:
        if interaction.guild.get_member(c["user_id"]):
            continue

        ok = await _add_user_to_guild(
            interaction.guild.id, c["user_id"], reason=f"JoinDev order #{order_id}"
        )
        if ok:
            await create_active_join(
                c["user_id"], interaction.guild.id, order_id, int(time.time())
            )
            added += 1

    await interaction.edit_original_response(
        content=(
            f"✅ **Order #{order_id} placed!**\n"
            f"Requested: **{to_order}** • Added: **{added}**\n"
            f"Coins spent: **{to_order}**\n\n"
            f"Members stay 3 days. Then I leave your server."
        )
    )


# ---------------------------------------------------------------
# ADMIN PREFIX COMMANDS
# ---------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID


@bot.command(name="join")
async def admin_join(ctx: commands.Context, guild_id: int, amount: str):
    if not is_admin(ctx.author.id):
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        await ctx.send(f"❌ Bot is not in server `{guild_id}`.")
        return

    async with get_pool().acquire() as conn:
        if amount.lower() == "max":
            rows = await conn.fetch("SELECT user_id FROM joindev.users WHERE do_not_join = FALSE")
        else:
            try:
                n = int(amount)
            except ValueError:
                await ctx.send("❌ Amount must be a number or 'max'.")
                return
            rows = await conn.fetch(
                "SELECT user_id FROM joindev.users WHERE do_not_join = FALSE LIMIT $1", n,
            )

    added = 0
    skipped = 0
    for r in rows:
        uid = r["user_id"]
        if guild.get_member(uid):
            skipped += 1
            continue

        ok = await _add_user_to_guild(guild_id, uid, reason="Admin .join")
        if ok:
            await create_active_join(uid, guild.id, None, int(time.time()))
            added += 1

    await ctx.send(f"✅ Added **{added}** to **{guild.name}**. Skipped: **{skipped}**")


@bot.command(name="give")
async def admin_give(ctx: commands.Context, user_id: int, amount: int):
    if not is_admin(ctx.author.id):
        return
    user = await get_user(user_id)
    if not user:
        await ctx.send(f"❌ User `{user_id}` not authorized.")
        return
    new_balance = await add_coins(user_id, amount)
    await ctx.send(f"✅ Gave **{amount}** to <@{user_id}>. New balance: **{new_balance}**")


@bot.command(name="check")
async def admin_check(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return

    try:
        async with get_pool().acquire() as conn:
            auth_count = await conn.fetchval("SELECT COUNT(*) FROM joindev.users")
            active_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM joindev.orders WHERE status = 'active'"
            )
            active_joins = await conn.fetchval(
                "SELECT COUNT(*) FROM joindev.active_joins WHERE rewarded = FALSE AND left_early = FALSE"
            )
            total_coins = await conn.fetchval(
                "SELECT COALESCE(SUM(joindev_coins), 0) FROM joindev.users"
            )
            total_servers = await conn.fetchval(
                "SELECT COUNT(*) FROM joindev.server_pool WHERE active = TRUE"
            )
            completed_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM joindev.orders WHERE status = 'completed'"
            )
            pending_welcome = await conn.fetchval(
                "SELECT COUNT(*) FROM joindev.pending_welcome WHERE sent = FALSE"
            )
    except Exception as e:
        await ctx.send(f"❌ **DB error:** `{type(e).__name__}: {e}`")
        log.error(f"admin_check: {e}")
        return

    embed = discord.Embed(title="📊 JoinDev Stats", color=0x5865F2)
    embed.add_field(name="👥 Authorized Users", value=f"**{auth_count}**", inline=True)
    embed.add_field(name="📦 Active Orders", value=f"**{active_orders}**", inline=True)
    embed.add_field(name="✅ Completed Orders", value=f"**{completed_orders}**", inline=True)
    embed.add_field(name="🔗 Pending Joins", value=f"**{active_joins}**", inline=True)
    embed.add_field(name="🌐 Active Servers", value=f"**{total_servers}**", inline=True)
    embed.add_field(name="🪙 Total JoinCoins Held", value=f"**{total_coins}**", inline=True)
    embed.add_field(name="📨 Pending Welcome DMs", value=f"**{pending_welcome}**", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="leave")
async def admin_leave(ctx: commands.Context, guild_id: int):
    if not is_admin(ctx.author.id):
        return
    if guild_id == SUPPORT_SERVER_ID:
        await ctx.send("❌ Refusing to leave the support server.")
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        await ctx.send(f"❌ Not in server `{guild_id}`.")
        return
    await guild.leave()
    await ctx.send(f"✅ Left **{guild.name}**.")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_BOT_TOKEN"))
