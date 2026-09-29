"""
alerts.py — Discord webhook alerts for JoinDev errors.

Reads ALERT_WEBHOOK_URL from environment and posts formatted embeds
whenever a log record at ERROR level or above passes through the root logger.
"""

import os
import time
import json
import logging
import threading
import traceback

import requests

log = logging.getLogger("joindev.alerts")

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")

# Throttle: one alert per unique title per N seconds
_THROTTLE_SECONDS = 60
_recent_alerts: dict[str, float] = {}
_throttle_lock = threading.Lock()

# Loggers whose errors should NOT trigger alerts
_IGNORED_LOGGERS = (
    "joindev.alerts",
    "discord.http",
    "discord.gateway",
    "werkzeug",
    "urllib3",
    "requests",
)


def _should_send(key: str) -> bool:
    """Returns True if we haven't sent this alert recently."""
    now = time.time()
    with _throttle_lock:
        last = _recent_alerts.get(key, 0)
        if now - last < _THROTTLE_SECONDS:
            return False
        _recent_alerts[key] = now
        return True


def send_alert(title: str, message: str, color: int = 0xED4245):
    """Sends a formatted embed to the Discord webhook (non-blocking)."""
    if not ALERT_WEBHOOK_URL:
        return

    if not _should_send(title):
        return

    def _do_send():
        try:
            payload = {
                "username": "JoinDev Alerts",
                "embeds": [{
                    "title": title[:256],
                    "description": message[:4000],
                    "color": color,
                    "footer": {"text": "JoinDev • Auto-alert"},
                }],
            }
            requests.post(
                ALERT_WEBHOOK_URL,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            # Never crash the app on alert failure
            log.warning(f"send_alert failed: {e}")

    threading.Thread(target=_do_send, daemon=True).start()


class WebhookAlertHandler(logging.Handler):
    """Logging handler that fires webhook alerts on ERROR+ records."""

    def emit(self, record: logging.LogRecord):
        # Skip noisy third-party loggers
        if record.name.startswith(_IGNORED_LOGGERS):
            return

        try:
            title = f"🚨 {record.name} — {record.levelname}"
            msg = record.getMessage()

            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
                msg = f"{msg}\n\n```\n{tb[:3400]}\n```"
            else:
                msg = f"```\n{msg[:3400]}\n```"

            color = 0xED4245 if record.levelno >= logging.ERROR else 0xF0B232
            send_alert(title, msg, color)
        except Exception:
            pass


def install_alert_handler():
    """Attaches the webhook alert handler to the root logger."""
    if not ALERT_WEBHOOK_URL:
        log.warning("install_alert_handler: ALERT_WEBHOOK_URL not set — alerts disabled")
        return

    handler = WebhookAlertHandler()
    handler.setLevel(logging.ERROR)
    logging.getLogger().addHandler(handler)
    log.info("install_alert_handler: webhook alerts enabled")
