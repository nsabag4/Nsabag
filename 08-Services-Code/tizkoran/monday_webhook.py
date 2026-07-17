"""Monday webhook reflexes: board events wake the department Claude agent.
Phase A pilot: Leads board -> Sales agent. See 04-Monday-Architecture/WEBHOOK-REFLEXES-PLAN.md."""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

import notify

log = logging.getLogger("tizkoran.reflex")

router = APIRouter()

AGENTS_DIR = "C:\\Sabag360-Framework\\07-Department-Agents"
ROUTES = {
    "1494474504": {
        "agent": "sales",
        "name": "סוכן המכירות",
        "cwd": os.path.join(AGENTS_DIR, "Sales-Agent"),
    },
}

DEDUP_WINDOW_SEC = 60
MAX_CONCURRENT = 2

_sem = threading.Semaphore(MAX_CONCURRENT)
_dedup_lock = threading.Lock()
_recent = {}


def _secret():
    return os.getenv("MONDAY_WEBHOOK_SECRET", "")


def _claude_bin():
    return os.getenv("CLAUDE_BIN", "").strip() or "claude"


def _timeout():
    try:
        return int(os.getenv("REFLEX_TIMEOUT_SEC", "300"))
    except (TypeError, ValueError):
        return 300


def _dedup(key: str) -> bool:
    now = time.time()
    with _dedup_lock:
        for k in [k for k, ts in _recent.items() if now - ts > DEDUP_WINDOW_SEC]:
            _recent.pop(k, None)
        if key in _recent:
            return False
        _recent[key] = now
        return True


def _run_reflex(route: dict, event: dict):
    with _sem:
        board_id = str(event.get("boardId", ""))
        pulse_id = str(event.get("pulseId", "") or event.get("itemId", ""))
        pulse_name = str(event.get("pulseName", "") or event.get("itemName", ""))
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        prompt = f"""אתה {route['name']} של סבג נדל"ן, מופעל כרפלקס אוטומטי מאירוע בלוח — נחי לא ליד המקלדת.

קרא קודם את AGENT.md בתיקייה הנוכחית — הזהות והחוקים שלך, כולל חוקי הברזל.

האירוע שהתקבל ממאנדיי (webhook):
{json.dumps(event, ensure_ascii=False, indent=1)}

ליד חדש בלוח הלידים ({board_id}), פריט {pulse_id} ("{pulse_name}"). בצע:
1. שלוף את הפריט המלא: python C:\\dev\\agent-gate\\monday_query.py עם שאילתת GraphQL על items (ids: {pulse_id}) כולל column_values.
2. בדוק את שלושת התנאים של כל ליד לפי החוקים שלך: בעלים | סטטוס עדכני | תאריך "פעולה הבאה" עתידי. מה שמותר לך להשלים — השלם בעדכון עמודות; מה שדורש את נחי — כתוב כדגל בעדכון.
3. כרטיס איש קשר: אם יש בליד טלפון/מייל — ודא כרטיס בלוח אנשי הקשר (1494474506) לפי חוק האין-כפילות (קודם חפש, רק אז צור).
4. כתוב עדכון (create_update) על הפריט {pulse_id}: סיכום קצר של מה בדקת ומה עשית, וחתום בסוף בדיוק כך:
"— {route['name']}, רפלקס אוטומטי, {stamp}"

אסור בהחלט: מחיקת פריטים | יצירת או מחיקת לוחות ועמודות | שינוי נתונים כספיים | פעולה מחוץ ללוחות שלך. עברית נקייה.
השורה האחרונה של הפלט שלך חייבת להיות: REFLEX_OK אם הכול הצליח, או REFLEX_FAIL: <סיבה>.
"""
        env = dict(os.environ)
        for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(var, None)
        log.info("reflex %s: spawning claude for item %s (%s)", route["agent"], pulse_id, pulse_name[:40])
        t0 = time.time()
        try:
            out = subprocess.run(
                [_claude_bin(), "-p", "--dangerously-skip-permissions"],
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=route["cwd"], env=env, timeout=_timeout(),
            )
            tail = (out.stdout or "").strip()[-400:]
            log.info("reflex %s: claude done rc=%s in %.0fs | %.200s",
                     route["agent"], out.returncode, time.time() - t0, tail)
            if out.returncode != 0 or "REFLEX_OK" not in (out.stdout or ""):
                notify.send(f"🚨 רפלקס {route['name']} נכשל על פריט {pulse_id} ({pulse_name}).\n{tail[:300]}")
        except subprocess.TimeoutExpired:
            log.error("reflex %s: timeout on item %s", route["agent"], pulse_id)
            notify.send(f"🚨 רפלקס {route['name']} חצה את מגבלת הזמן על פריט {pulse_id} ({pulse_name}).")
        except Exception:
            log.exception("reflex %s: crashed", route["agent"])
            notify.send(f"🚨 רפלקס {route['name']} קרס על פריט {pulse_id}. ראה יומן תזכורן.")


@router.post("/monday/webhook")
async def monday_webhook(request: Request):
    if not _secret() or request.query_params.get("secret") != _secret():
        raise HTTPException(status_code=403, detail="bad secret")
    body = await request.json()
    if "challenge" in body:
        return {"challenge": body["challenge"]}
    event = body.get("event") or {}
    board_id = str(event.get("boardId", ""))
    route = ROUTES.get(board_id)
    if not route:
        log.info("reflex: no route for board %s (ignored)", board_id)
        return {"ok": True, "routed": False}
    pulse_id = str(event.get("pulseId", "") or event.get("itemId", ""))
    key = f"{board_id}:{pulse_id}:{event.get('type', '')}"
    if not _dedup(key):
        log.info("reflex: duplicate within %ss ignored (%s)", DEDUP_WINDOW_SEC, key)
        return {"ok": True, "dedup": True}
    threading.Thread(target=_run_reflex, args=(route, event), daemon=True,
                     name=f"reflex-{pulse_id}").start()
    return {"ok": True, "routed": True}