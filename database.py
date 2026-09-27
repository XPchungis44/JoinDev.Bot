"""
database.py — Async Postgres for JoinDev
Uses 'joindev' schema to avoid conflicts with other bots.
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


async def init_pool():
    """Creates the connection pool and initializes the schema."""
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

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
                do_not_join      INTEGER DEFAULT 0
            )
        """)
    log.info("Database pool ready, schema initialized.")


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def upsert_user(user_id: int, access_token: str, refresh_token: str, expires_at: int):
    """Inserts a new user or updates their tokens."""
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO joindev.users (user_id, access_token, refresh_token, token_expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires_at = EXCLUDED.token_expires_at
        """, user_id, access_token, refresh_token, expires_at)


async def get_user(user_id: int) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM joindev.users WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def get_all_users() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM joindev.users")
        return [dict(r) for r in rows]


async def update_tokens(user_id: int, access_token: str, refresh_token: str, expires_at: int):
    async with _pool.acquire() as conn:
        await conn.execute("""
            UPDATE joindev.users
            SET access_token = $2, refresh_token = $3, token_expires_at = $4
            WHERE user_id = $1
        """, user_id, access_token, refresh_token, expires_at)


async def update_coins(user_id: int, coins: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET joindev_coins = $2 WHERE user_id = $1",
            user_id, coins,
        )


async def set_do_not_join(user_id: int, flag: bool):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE joindev.users SET do_not_join = $2 WHERE user_id = $1",
            user_id, 1 if flag else 0,
        )
