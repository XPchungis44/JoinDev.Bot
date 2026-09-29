"""
database.py — Async Postgres for JoinDev
"""

import os
import time
import asyncpg
import logging

log = logging.getLogger("joindev.db")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

_pool: asyncpg.Pool | None = None

THREE_DAYS = 3 * 24 * 60 * 60


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    async with _pool.acquire() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS joindev")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joindev.users (
                user_id          BIGINT PRIMARY KEY,
                access_token     TEXT,
                refresh_token    TEXT,
                token_expires_at BIGINT,
                joindev_coins    INTEGER DEFAULT 10,
                daily_streak     INTEGER DEFAULT 0,
                last_daily       BIGINT DEFAULT 0,
                do_not_join      INTEGER DEFAULT 0,
                last_auto_join   BIGINT DEFAULT 0,
                last_buy_members BIGINT DEFAULT 0,
                banned           BOOLEAN DEFAULT FALSE
            )
        """)

        # Add missing columns if table already existed
        for col, definition in [
            ("last_auto_join", "BIGINT DEFAULT 0"),
            ("last_buy_members", "BIGINT DEFAULT 0"),
            ("banned", "BOOLEAN DEFAULT FALSE"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE joindev.users ADD COLUMN IF NOT EXISTS {col} {definition}"
                )
            except Exception:
                pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joindev.server_pool (
                guild_id     BIGINT PRIMARY KEY,
                owner_id     BIGINT NOT NULL,
                invite_code  TEXT,
                added_at     BIGINT NOT NULL,
                active       BOOLEAN DEFAULT TRUE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joindev.orders (
                order_id           SERIAL PRIMARY KEY,
                guild_id           BIGINT NOT NULL,
                owner_id           BIGINT NOT NULL,
                members_requested  INTEGER NOT NULL,
                members_completed  INTEGER DEFAULT 0,
                coins_spent        INTEGER NOT NULL,
                created_at         BIGINT NOT NULL,
                completed_at       BIGINT,
                status             TEXT DEFAULT 'active'
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joindev.active_joins (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT NOT NULL,
                guild_id     BIGINT NOT NULL,
                order_id     INTEGER,
                joined_at    BIGINT NOT NULL,
                rewarded     BOOLEAN DEFAULT FALSE,
                left_early   BOOLEAN DEFAULT FALSE,
                checked_at   BIGINT
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_joins_pending
            ON joindev.active_joins (rewarded, left_early, joined_at)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS joindev.pending_welcome (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                created_at BIGINT NOT NULL,
                sent       BOOLEAN DEFAULT FALSE
            )
        """)

    log.info("Database pool ready, schema initialized.")


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized.")
    return _pool


# ---------------------------------------------------------------
# USERS
# ---------------------------------------------------------------
async def upsert_user(user_id, access_token, refresh_token, expires_at):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO joindev.users (user_id, access_token, refresh_token, token_expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires_at = EXCLUDED.token_expires_at
        """, user_id, access_token, refresh_token, expires_at)


async def get_user(user_id):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM joindev.users WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def get_all_users():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM joindev.users")
        return [dict(r) for r in rows]


async def update_tokens(user_id, access_token, refresh_token, expires_at):
    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE joindev.users
            SET access_token = $2, refresh_token = $3, token_expires_at = $4
            WHERE user_id = $1
        """, user_id, access_token, refresh_token, expires_at)


async def update_coins(user_id, coins):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET joindev_coins = $2 WHERE user_id = $1",
            user_id, coins,
        )


async def add_coins(user_id, amount):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE joindev.users
            SET joindev_coins = joindev_coins + $2
            WHERE user_id = $1
            RETURNING joindev_coins
        """, user_id, amount)
        return row["joindev_coins"] if row else 0


async def set_do_not_join(user_id, flag):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET do_not_join = $2 WHERE user_id = $1",
            user_id, 1 if flag else 0,
        )


async def set_banned(user_id, banned: bool):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET banned = $2 WHERE user_id = $1",
            user_id, banned,
        )


async def touch_auto_join(user_id, now):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET last_auto_join = $2 WHERE user_id = $1",
            user_id, now,
        )


async def touch_buy_members(user_id, now):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET last_buy_members = $2 WHERE user_id = $1",
            user_id, now,
        )


async def claim_daily(user_id, now):
    COOLDOWN = 24 * 60 * 60
    BASE_REWARD = 3
    MAX_COINS = 10000

    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT joindev_coins, daily_streak, last_daily FROM joindev.users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not row:
                return {"success": False, "reason": "no_user"}

            last_daily = row["last_daily"] or 0
            streak = row["daily_streak"] or 0
            balance = row["joindev_coins"] or 0
            elapsed = now - last_daily

            if elapsed < COOLDOWN:
                return {"success": False, "reason": "cooldown", "seconds_left": COOLDOWN - elapsed}

            new_streak = streak + 1 if elapsed < COOLDOWN * 2 else 1
            coins_awarded = BASE_REWARD + (new_streak - 1)
            new_balance = min(balance + coins_awarded, MAX_COINS)

            await conn.execute("""
                UPDATE joindev.users
                SET joindev_coins = $2, daily_streak = $3, last_daily = $4
                WHERE user_id = $1
            """, user_id, new_balance, new_streak, now)

            return {
                "success": True,
                "coins_awarded": new_balance - balance,
                "new_streak": new_streak,
                "new_balance": new_balance,
            }


# ---------------------------------------------------------------
# SERVER POOL
# ---------------------------------------------------------------
async def add_server(guild_id, owner_id, invite_code, now):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO joindev.server_pool (guild_id, owner_id, invite_code, added_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id) DO UPDATE SET
                invite_code = EXCLUDED.invite_code,
                owner_id = EXCLUDED.owner_id,
                active = TRUE
        """, guild_id, owner_id, invite_code, now)


async def remove_server(guild_id):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.server_pool SET active = FALSE WHERE guild_id = $1", guild_id,
        )


async def get_active_servers(exclude_guild_ids=None):
    async with _pool.acquire() as conn:
        if exclude_guild_ids:
            rows = await conn.fetch("""
                SELECT * FROM joindev.server_pool
                WHERE active = TRUE AND guild_id <> ALL($1::bigint[])
            """, exclude_guild_ids)
        else:
            rows = await conn.fetch("SELECT * FROM joindev.server_pool WHERE active = TRUE")
        return [dict(r) for r in rows]


# ---------------------------------------------------------------
# ORDERS
# ---------------------------------------------------------------
async def create_order(guild_id, owner_id, members, coins, now):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO joindev.orders
                (guild_id, owner_id, members_requested, coins_spent, created_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING order_id
        """, guild_id, owner_id, members, coins, now)
        return row["order_id"]


async def get_order(order_id):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM joindev.orders WHERE order_id = $1", order_id)
        return dict(row) if row else None


async def increment_order_completed(order_id):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE joindev.orders
            SET members_completed = members_completed + 1
            WHERE order_id = $1
            RETURNING *
        """, order_id)
        return dict(row) if row else {}


async def complete_order(order_id, now):
    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE joindev.orders
            SET status = 'completed', completed_at = $2
            WHERE order_id = $1
        """, order_id, now)


# ---------------------------------------------------------------
# ACTIVE JOINS
# ---------------------------------------------------------------
async def create_active_join(user_id, guild_id, order_id, now):
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO joindev.active_joins (user_id, guild_id, order_id, joined_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, user_id, guild_id, order_id, now)
        return row["id"]


async def get_pending_joins():
    cutoff = int(time.time()) - THREE_DAYS
    async with _pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM joindev.active_joins
            WHERE rewarded = FALSE AND left_early = FALSE AND joined_at <= $1
        """, cutoff)
        return [dict(r) for r in rows]


async def mark_join_rewarded(join_id):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.active_joins SET rewarded = TRUE, checked_at = $2 WHERE id = $1",
            join_id, int(time.time()),
        )


async def mark_join_left_early(join_id):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.active_joins SET left_early = TRUE, checked_at = $2 WHERE id = $1",
            join_id, int(time.time()),
        )


async def get_user_active_joins(user_id):
    async with _pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM joindev.active_joins
            WHERE user_id = $1 AND rewarded = FALSE AND left_early = FALSE
        """, user_id)
        return [dict(r) for r in rows]


# ---------------------------------------------------------------
# PENDING WELCOME
# ---------------------------------------------------------------
async def queue_welcome_dm(user_id):
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO joindev.pending_welcome (user_id, created_at)
            VALUES ($1, $2)
        """, user_id, int(time.time()))


async def get_pending_welcome_dms():
    async with _pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM joindev.pending_welcome
            WHERE sent = FALSE
            ORDER BY created_at ASC
            LIMIT 50
        """)
        return [dict(r) for r in rows]


async def mark_welcome_sent(pending_id):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.pending_welcome SET sent = TRUE WHERE id = $1", pending_id,
        )
