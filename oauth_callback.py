"""
oauth_callback.py — Handles Discord OAuth2 callback
Exchanges the authorization code for a user access token,
stores the user in Postgres, and queues the welcome DM.
"""

import os
import time
import logging

import requests
from flask import Flask, request, jsonify

from database import init_db, upsert_user

log = logging.getLogger("joindev.oauth")

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")

API_ENDPOINT = "https://discord.com/api/v10"

oauth_app = Flask(__name__)

# Welcome DM queue file (read by bot.py)
WELCOME_QUEUE_FILE = "/tmp/joindev_welcome_queue.txt"


# ---------------------------------------------------------------
# TOKEN EXCHANGE
# ---------------------------------------------------------------
def exchange_code(code: str) -> dict | None:
    """Exchanges an authorization code for an access token."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(
            f"{API_ENDPOINT}/oauth2/token",
            data=data,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        log.error(f"Token exchange failed: {e}")
        return None


def fetch_user_id(access_token: str) -> str | None:
    """Fetches the authenticated user's Discord ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(
            f"{API_ENDPOINT}/users/@me",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("id")
    except requests.RequestException as e:
        log.error(f"Failed to fetch user: {e}")
        return None


# ---------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------
@oauth_app.route("/callback")
def callback():
    """Discord redirects here after the user authorizes."""
    code = request.args.get("code")

    if not code:
        log.warning("Callback hit without a code parameter.")
        return jsonify({"error": "missing_code"}), 400

    token_data = exchange_code(code)
    if not token_data:
        return jsonify({"error": "token_exchange_failed"}), 500

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 604800)  # 7 days default
    expires_at = int(time.time()) + expires_in

    user_id = fetch_user_id(access_token)
    if not user_id:
        return jsonify({"error": "failed_to_fetch_user"}), 500

    log.info(f"OAuth complete for user {user_id}")

    # Store user in Postgres
    try:
        upsert_user(user_id, access_token, refresh_token, expires_at)
        log.info(f"Stored user {user_id} in database")
    except Exception as e:
        log.error(f"Database error for {user_id}: {e}")
        return jsonify({"error": "database_error"}), 500

    # Queue the welcome DM for the bot to send
    _queue_welcome_dm(user_id)

    return """
    <!DOCTYPE html>
    <html>
    <head><title>JoinDev — Authorized</title></head>
    <body style="background:#0b0d12;color:#e6e9ef;font-family:sans-serif;text-align:center;padding:60px;">
        <h1 style="color:#5865F2;">✅ Authorized!</h1>
        <p>Check your Discord DMs — I just sent you a welcome message.</p>
        <p><a href="https://XPchungis44.github.io/JoinDev/" style="color:#7c85ff;">← Back to JoinDev</a></p>
    </body>
    </html>
    """, 200


def _queue_welcome_dm(user_id: str):
    """Appends a user ID to the welcome queue file."""
    try:
        with open(WELCOME_QUEUE_FILE, "a") as f:
            f.write(f"{user_id}\n")
        log.info(f"Queued welcome DM for user {user_id}")
    except IOError as e:
        log.error(f"Failed to queue welcome DM: {e}")
