"""
database.py — Postgres setup for JoinDev
Requires: psycopg2-binary (add to requirements.txt)
Uses the 'joindev' schema to avoid conflicts with other bots.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")


def get_connection():
    """Returns a new Postgres connection with dict-style rows."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Creates the joindev schema and users table if they don't exist."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("CREATE SCHEMA IF NOT EXISTS joindev")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS joindev.users (
            user_id          TEXT PRIMARY KEY,
            access_token     TEXT,
            refresh_token    TEXT,
            token_expires_at BIGINT,
            joindev_coins    INTEGER DEFAULT 10,
            daily_streak     INTEGER DEFAULT 0,
            last_daily       BIGINT DEFAULT 0,
            do_not_join      INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_user(user_id: str) -> dict | None:
    """Fetches a user row by Discord ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM joindev.users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def upsert_user(user_id: str, access_token: str, refresh_token: str, expires_at: int):
    """
    Inserts a new user or updates tokens if they already exist.
    Coins and streak are preserved on update.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO joindev.users (user_id, access_token, refresh_token, token_expires_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            token_expires_at = EXCLUDED.token_expires_at
    """, (user_id, access_token, refresh_token, expires_at))
    conn.commit()
    cur.close()
    conn.close()
