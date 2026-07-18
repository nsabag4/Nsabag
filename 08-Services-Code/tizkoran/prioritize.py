"""Smart pick of drive-time callbacks via the local Claude CLI (no API key needed)."""
import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger("tizkoran.prioritize")


def _claude_bin():
    configured = os.getenv("CLAUDE_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("claude")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def smart_pick(items, minutes, n_calls):
    """Rank pending tasks for a drive and explain each pick.

    Returns at most n_calls dicts: {"name", "phone", "reason"}.
    Returns None on any failure so the caller falls back to the plain list.
    """
    try:
        return _smart_pick(items, minutes, n_calls)
    except Exception:
        log.exception("smart_pick failed; falling back to plain order")
        return None


def _smart_pick(items, minutes, n_calls):
    if os.getenv("SMART_PRIORITY", "1") != "1":
        return None
    if not items or len(items) <= 1:
        return None
    binary = _claude_bin()
    if not binary:
        log.warning("smart_pick: claude CLI not found (set CLAUDE_BIN in .env)")
        return None

    lines = []
    for i, it in enumerate(items, start=1):
        parts = [f"{i}. {it.get('name', '')}"]
        if it.get("urgency"):
            parts.append(f"דחיפות: {it['urgency']}")
        if it.get("due"):
            parts.append(f"תאריך יעד: {it['due']}")
        if it.get("status"):
            parts.append(f"סטטוס: {it['status']}")
        lines.append(" | ".join(parts))

    prompt = (
        "אתה מתעדף שיחות טלפון לעורך דין בזמן נסיעה ברכב.\n"
        f"אורך הנסיעה: כ-{int(minutes)} דקות. יש זמן ל-{int(n_calls)} שיחות לכל היותר.\n"
        "אלה המשימות הפתוחות שלו:\n"
        + "\n".join(lines) + "\n"
        "בחר את הדחופות באמת, לפי הסדר הזה: דחיפות קריטי/דחוף קודם, "
        "אחר כך תאריך יעד שעבר או קרוב, ואחר כך שיקול דעת לפי תוכן המשימה. "
        "משימה שאינה שיחה או פנייה לאדם (למשל ניסוח מסמך) — דלג עליה.\n"
        'החזר JSON בלבד, בלי הסברים מסביב, בתבנית: '
        '{"picks":[{"i":<מספר מהרשימה>,"why":"<נימוק של עד 6 מילים בעברית>"}]}'
    )

    timeout = _int_env("SMART_PRIORITY_TIMEOUT_SEC", 60)
    out = subprocess.run(
        [binary, "-p", prompt],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    raw = (out.stdout or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        log.warning("smart_pick: no JSON in claude output: %.200s", raw)
        return None
    data = json.loads(raw[start:end + 1])

    picks = []
    seen = set()
    for p in data.get("picks", []):
        try:
            idx = int(p.get("i", 0)) - 1
        except (TypeError, ValueError):
            continue
        if idx in seen or not (0 <= idx < len(items)):
            continue
        seen.add(idx)
        it = items[idx]
        reason = " ".join(str(p.get("why", "")).split())  # flatten model newlines
        picks.append({
            "name": it.get("name", ""),
            "phone": it.get("phone", ""),
            "reason": reason[:60],
        })
        if len(picks) >= n_calls:
            break
    return picks or None
