"""
main.py — Render entrypoint for JoinDev
Flask runs on $PORT (Render requirement). Bot runs in a thread.
"""

import os
import asyncio
import logging
import threading

from flask import Flask, jsonify

from oauth_callback import oauth_bp
from database import init_pool

log = logging.getLogger("joindev")

# ---------------------------------------------------------------
# HEALTH CHECK + OAUTH ROUTES
# ---------------------------------------------------------------
health_app = Flask(__name__)


@health_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

@health_app.route("/")
def root():
    return jsonify({
        "status": "online",
        "service": "JoinDev Bot",
        "message": "Server growth made easy!"
    }), 200
health_app.register_blueprint(oauth_bp)


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Init DB pool ONCE on the bot's eventual loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_pool())
    # Do NOT close this loop — the bot will reuse it

    # Start the bot on that same loop
    threading.Thread(target=start_bot, daemon=True).start()

    # Run Flask on Render's port (blocking)
    port = int(os.getenv("PORT", 10000))
    log.info(f"Starting Flask on port {port}")
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)
