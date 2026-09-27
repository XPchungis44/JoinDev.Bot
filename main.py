"""
main.py — Render entrypoint for JoinDev
Flask runs on $PORT (Render requirement). Bot runs in a thread.
"""

import os
import asyncio
import logging
import threading

from flask import Flask, jsonify

from oauth_callback import oauth_app
from database import init_pool, get_all_users, update_tokens

log = logging.getLogger("joindev")

# ---------------------------------------------------------------
# HEALTH CHECK (Render routes traffic to $PORT)
# ---------------------------------------------------------------
health_app = Flask(__name__)

@health_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

# Mount the OAuth routes onto the health app so everything is on one port
health_app.register_blueprint(oauth_app)


# ---------------------------------------------------------------
# TOKEN REFRESH (Discord tokens expire in 7 days)
# ---------------------------------------------------------------
def refresh_tokens_sync():
    """Synchronous token refresh — called on a timer."""
    import requests, time
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        users = loop.run_until_complete(get_all_users())
    finally:
        loop.close()

    now = int(time.time())
    for u in users:
        if u["token_expires_at"] and u["token_expires_at"] - now < 86400:  # <1 day left
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
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(update_tokens(
                        u["user_id"],
                        new["access_token"],
                        new["refresh_token"],
                        int(time.time()) + new.get("expires_in", 604800),
                    ))
                finally:
                    loop.close()
                log.info(f"Refreshed tokens for {u['user_id']}")
            except Exception as e:
                log.error(f"Refresh failed for {u['user_id']}: {e}")


def start_refresh_timer():
    """Runs refresh_tokens_sync once every 12 hours in a thread."""
    def loop_forever():
        while True:
            try:
                refresh_tokens_sync()
            except Exception as e:
                log.error(f"Refresh loop error: {e}")
            threading.Event().wait(60 * 60 * 12)  # 12 hours

    threading.Thread(target=loop_forever, daemon=True).start()


# ---------------------------------------------------------------
# BOT RUNNER
# ---------------------------------------------------------------
def start_bot():
    import bot
    bot.bot.run(os.getenv("DISCORD_BOT_TOKEN"))


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Init DB pool
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_pool())
    loop.close()
    log.info("Database pool initialized.")

    # Start bot in a background thread
    threading.Thread(target=start_bot, daemon=True).start()

    # Start token refresh timer
    start_refresh_timer()

    # Run Flask on Render's port (blocking)
    port = int(os.getenv("PORT", 10000))
    log.info(f"Starting Flask on port {port}")
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)
