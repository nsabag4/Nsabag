"""Watch the Agent Gate heartbeat and tell Nachi when the bots go silent."""
import logging
import os
import time

import notify
import state

log = logging.getLogger("tizkoran.guard")

_HEARTBEAT = os.getenv("GATE_HEARTBEAT_PATH", r"C:\dev\agent-gate\heartbeat.txt")
_MAX_AGE_SEC = 900  # heartbeat is written every watchdog cycle (default 10 min)


def check_agent_gate():
    try:
        _check_agent_gate()
    except Exception:  # never let the scheduler die
        log.exception("check_agent_gate failed")


def _check_agent_gate():
    if not os.path.exists(_HEARTBEAT):
        return  # gate never ran on this machine — nothing to watch yet
    age = time.time() - os.path.getmtime(_HEARTBEAT)
    ok = age < _MAX_AGE_SEC
    prev = state.get("agent_gate_ok")
    if prev is True and not ok:
        notify.send(
            "🚨 התראת מערכת: שער הבוטים של הסוכנים הפסיק להגיב.\n"
            "הבוטים בטלגרם לא יענו עד שיופעל מחדש (קיצור Agent Gate בתיקיית ההפעלה, או הפעלת מחשב מחדש)."
        )
    elif prev is False and ok:
        notify.send("✅ שער הבוטים חזר לעבוד.")
    state.set("agent_gate_ok", ok)
