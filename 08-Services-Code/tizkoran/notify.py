"""Send notifications to the user via Telegram."""
import logging
import os

import requests

log = logging.getLogger("tizkoran.notify")


def send(text: str, buttons=None) -> bool:
    """Send a Telegram message. `buttons` is an optional inline keyboard:
    a list of rows, each row a list of {"text", "callback_data"} dicts."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram not configured. Message was:\n%s", text)
        return False
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False
