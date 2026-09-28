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

    # Start the bot — it will init the DB pool on its own loop
    threading.Thread(target=start_bot, daemon=True).start()

    # Run Flask on Render's port (blocking)
    port = int(os.getenv("PORT", 10000))
    log.info(f"Starting Flask on port {port}")
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)
