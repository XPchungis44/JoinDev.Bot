"""
oauth_callback.py — Discord OAuth2 callback
Exchanges the code for tokens, then writes them to a queue file.
The bot reads the queue and writes to Postgres on its own event loop.
"""

import os
import time
import json
import logging

import requests
from flask import Blueprint, request, jsonify

log = logging.getLogger("joindev.oauth")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")

API_ENDPOINT = "https://discord.com/api/v10"

oauth_bp = Blueprint("oauth", __name__)

# Files read by bot.py
TOKEN_QUEUE_FILE = "/tmp/joindev_token_queue.jsonl"
WELCOME_QUEUE_FILE = "/tmp/joindev_welcome_queue.txt"


def exchange_code(code: str) -> dict | None:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(f"{API_ENDPOINT}/oauth2/token", data=data, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.error(f"Token exchange failed: {e}")
        return None


def fetch_user_id(access_token: str) -> int | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(f"{API_ENDPOINT}/users/@me", headers=headers, timeout=10)
        r.raise_for_status()
        return int(r.json().get("id"))
    except requests.RequestException as e:
        log.error(f"Failed to fetch user: {e}")
        return None


@oauth_bp.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "missing_code"}), 400

    token_data = exchange_code(code)
    if not token_data:
        return jsonify({"error": "token_exchange_failed"}), 500

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 604800)
    expires_at = int(time.time()) + expires_in

    user_id = fetch_user_id(access_token)
    if not user_id:
        return jsonify({"error": "failed_to_fetch_user"}), 500

    # Write to token queue — bot will pick this up and insert on its own loop
    try:
        entry = {
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        with open(TOKEN_QUEUE_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        log.info(f"Queued token data for user {user_id}")
    except IOError as e:
        log.error(f"Failed to write token queue: {e}")
        return jsonify({"error": "queue_write_failed"}), 500

    # Queue the welcome DM
    try:
        with open(WELCOME_QUEUE_FILE, "a") as f:
            f.write(f"{user_id}\n")
    except IOError as e:
        log.error(f"Failed to queue welcome DM: {e}")

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>JoinDev — Authorized</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    </head>
    <body style="background:#0b0d12;color:#e6e9ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;text-align:center;padding:80px 24px;">
        <h1 style="color:#5865F2;font-size:2rem;">✅ Authorized!</h1>
        <p style="color:#8b93a7;">Check your Discord DMs — I just sent you a welcome message.</p>
        <p style="margin-top:32px;">
            <a href="https://XPchungis44.github.io/JoinDev/" style="color:#7c85ff;text-decoration:none;font-weight:600;">
                ← Back to JoinDev
            </a>
        </p>
    </body>
    </html>
    """, 200
