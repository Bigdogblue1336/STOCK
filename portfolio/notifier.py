"""Optional push notifications for the day's alerts. Off by default.

Two channels, both no-ops unless explicitly enabled:
  - telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars.
  - imessage: macOS only, via `osascript` to a recipient (IMESSAGE_RECIPIENT
    env var, or config.DEFAULT_IMESSAGE_RECIPIENT).

The runner calls maybe_send(alerts, asof) unconditionally at the end of every
run; this module is what decides whether anything actually goes out, based
on config.DEFAULT_NOTIFIER_CHANNEL ('none' by default) or an explicit
`channel` argument.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Optional

from . import config

logger = logging.getLogger(__name__)

DISABLED_VALUES = {"", "none", "off", "disabled"}


def _format_message(alerts: list[dict[str, Any]], asof: date) -> str:
    if not alerts:
        return f"Portfolio monitor {asof.isoformat()}: no alerts today."
    lines = [f"Portfolio monitor alerts for {asof.isoformat()} ({len(alerts)}):"]
    for a in alerts:
        lines.append(f"[{a['severity'].upper()}] {a['message']}")
    return "\n".join(lines)


def _send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram notifier enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        logger.error("Telegram notification failed: %s", exc)
        return False


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _send_imessage(text: str, recipient: str) -> bool:
    if not recipient:
        logger.warning("iMessage notifier enabled but no recipient configured (IMESSAGE_RECIPIENT); skipping.")
        return False

    script = (
        f'tell application "Messages" to send "{_escape_applescript(text)}" '
        f'to buddy "{recipient}" of (service 1 whose service type is iMessage)'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)
        return True
    except FileNotFoundError:
        logger.warning("iMessage notifier requires macOS (osascript not found); skipping.")
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("iMessage notification failed: %s", exc)
        return False


def maybe_send(
    alerts: list[dict[str, Any]],
    asof: date,
    channel: Optional[str] = None,
    imessage_recipient: Optional[str] = None,
) -> bool:
    """No-op unless a channel is explicitly enabled. Returns True iff a message was sent."""
    channel = (channel if channel is not None else config.DEFAULT_NOTIFIER_CHANNEL).strip().lower()
    if channel in DISABLED_VALUES:
        return False

    text = _format_message(alerts, asof)

    if channel == "telegram":
        return _send_telegram(text)
    if channel == "imessage":
        recipient = imessage_recipient or config.DEFAULT_IMESSAGE_RECIPIENT
        return _send_imessage(text, recipient)

    logger.warning("Unknown notifier channel %r; no-op.", channel)
    return False
