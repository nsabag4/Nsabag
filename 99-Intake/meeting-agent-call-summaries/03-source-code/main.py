# -*- coding: utf-8 -*-
"""
סוכן יומן — סבג נדל"ן · v2.2 (מהדורת טלגרם)

שירות שקולט אינטראקציות עסקיות משני מקורות:
  א. בוט טלגרם ("הסוכן של נחי") — טקסט, הודעות קוליות, קבצי אודיו משותפים.
  ב. תיקיית קליטה מקומית (INBOX_PATH) — הקלטות שיחה מהסמסונג דרך Google Drive.

לכל קלט: תמלול (Groq Whisper) ← חילוץ מובנה (claude -p) ← שער אישור בטלגרם
(להקלטות שיחה בלבד) ← כתיבה למאנדיי (חדר המיון + לוח המשימות) ← אישור בטלגרם.

מפרט מחייב: meeting-agent-spec.md (v2.2). אין קלט שנעלם בשקט.
"""

import atexit
import html
import json
import logging
import msvcrt
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("חסרה חבילת requests — הרץ: py -m pip install -r requirements.txt")
    sys.exit(1)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_OK = True
except ImportError:
    WATCHDOG_OK = False

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"
LOCK_PATH = BASE_DIR / "agent.lock"
LOGS_DIR = BASE_DIR / "logs"
TMP_DIR = BASE_DIR / "tmp"
PROCESSED_DIR = BASE_DIR / "processed"
FAILED_DIR = BASE_DIR / "failed"

VERSION = "2.2.0"

# ---------------------------------------------------------------- הגדרות

def _load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env

_ENV_FILE = _load_env_file(BASE_DIR / ".env")

def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV_FILE.get(key) or default

BOT_TOKEN = cfg("BOT_TOKEN")
NAHI_TELEGRAM_ID = cfg("NAHI_TELEGRAM_ID")
GROQ_API_KEY = cfg("GROQ_API_KEY")
MONDAY_API_TOKEN = cfg("MONDAY_API_TOKEN")

INTAKE_BOARD_ID = cfg("MONDAY_INTAKE_BOARD_ID", "5094858663")
TASKS_BOARD_ID = cfg("MONDAY_TASKS_BOARD_ID", "5094863814")
CONTACTS_BOARD_ID = cfg("MONDAY_CONTACTS_BOARD_ID", "1494474506")
FILES_BOARD_ID = cfg("MONDAY_FILES_BOARD_ID", "5099714404")
DEDUP_TASKS = cfg("DEDUP_TASKS", "yes").strip().lower() in ("yes", "true", "1")
LOOKUP_CLIENT = cfg("LOOKUP_CLIENT", "yes").strip().lower() in ("yes", "true", "1")
INTAKE_GROUP_TITLE = cfg("INTAKE_GROUP_TITLE", "🎙️ סיכומי שיחות")
TASKS_GROUP_TITLE = cfg("TASKS_GROUP_TITLE", "📥 נכנס מהסוכן")
TASK_INITIAL_STATUS = cfg("TASK_INITIAL_STATUS", "חדש")
# חדר המיון הוא לוח חי של נחי — יצירת הקבוצה בו דורשת אישור מפורש (המבנה קפוא)
INTAKE_GROUP_AUTOCREATE = cfg("INTAKE_GROUP_AUTOCREATE", "no").strip().lower() in ("yes", "true", "1")

INBOX_PATH = Path(cfg("INBOX_PATH", str(BASE_DIR / "inbox")))
# קבצים שתאריכם מוקדם מזה נרשמים ומדולגים — מגן מפני מבול הקלטות ישנות בסנכרון הראשון
INBOX_MIN_DATE = cfg("INBOX_MIN_DATE", "")
APPROVAL_CHANNELS = {c.strip() for c in cfg("APPROVAL_CHANNELS", "שיחה").split(",") if c.strip()}
RESCAN_SECONDS = int(cfg("RESCAN_SECONDS", "120"))
REMINDER_HOURS = float(cfg("REMINDER_HOURS", "4"))

CLAUDE_BIN = cfg("CLAUDE_BIN")
FFMPEG_BIN = cfg("FFMPEG_BIN", "ffmpeg")
GROQ_MODEL = cfg("GROQ_MODEL", "whisper-large-v3")

def _cfg_json(key: str, default):
    raw = cfg(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default

COLUMN_MAP_INTAKE = _cfg_json("COLUMN_MAP_INTAKE", {})
COLUMN_MAP_TASKS = _cfg_json("COLUMN_MAP_TASKS", {})
PRIORITY_MAP = _cfg_json("PRIORITY_MAP", {"דחוף": "דחוף", "גבוה": "דחוף", "רגיל": "רגיל"})

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MONDAY_URL = "https://api.monday.com/v2"

AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".wav", ".amr", ".ogg", ".oga", ".aac", ".flac", ".mp4"}
GROQ_SUPPORTED = {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".webm"}
GROQ_MAX_BYTES = 24 * 1024 * 1024
TG_MAX_DOWNLOAD = 20 * 1024 * 1024

# ---------------------------------------------------------------- לוג יומי

class DailyFileHandler(logging.Handler):
    """כותב ל-logs/YYYY-MM-DD.log ומחליף קובץ אוטומטית בחצות."""

    def __init__(self):
        super().__init__()
        self._day = None
        self._fh = None

    def emit(self, record):
        try:
            today = date.today().isoformat()
            if today != self._day:
                if self._fh:
                    self._fh.close()
                LOGS_DIR.mkdir(exist_ok=True)
                self._fh = open(LOGS_DIR / f"{today}.log", "a", encoding="utf-8")
                self._day = today
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:
            pass

log = logging.getLogger("agent")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
_h1 = DailyFileHandler(); _h1.setFormatter(_fmt); log.addHandler(_h1)
_h2 = logging.StreamHandler(); _h2.setFormatter(_fmt); log.addHandler(_h2)

# ---------------------------------------------------------------- מצב מתמיד

STATE_LOCK = threading.RLock()
STATE = {
    "offset": 0,            # update_id האחרון שנמשך מטלגרם
    "chat_id": None,
    "chat_confirmed": False,
    "processed_files": {},  # "שם|גודל" -> חותמת עיבוד (נרשם רק בסיום מלא)
    "pending": {},          # פריטים בשער האישור
    "jobs": {},             # יומן עבודות עמיד — עבודה שנקטעה משוחזרת בעלייה
    "last_alive": None,
}

def load_state():
    global STATE
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            STATE.update(loaded)
        except Exception as e:
            log.error(f"state.json פגום — ממשיך עם מצב נקי ({e})")

def save_state():
    with STATE_LOCK:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_PATH)

# ---------------------------------------------------------------- טלגרם

class TgError(Exception):
    pass

def tg(method: str, http_timeout: int = 30, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=http_timeout)
    try:
        data = r.json()
    except Exception:
        raise TgError(f"{method}: HTTP {r.status_code}")
    if not data.get("ok"):
        raise TgError(f"{method}: {data.get('description', 'שגיאה לא ידועה')}")
    return data.get("result")

def _tg_esc(text) -> str:
    """Escape for Telegram HTML parse_mode (no <br>, real newlines)."""
    return html.escape(str(text or ""))


def notify(text: str, reply_to=None, keyboard=None, silent_fail=True, parse_mode=None):
    """שולח הודעה לנחי. אין עדיין chat_id — נרשם ללוג בלבד."""
    chat_id = STATE.get("chat_id")
    if not chat_id:
        log.warning(f"אין chat_id — הודעה לא נשלחה: {text[:120]}")
        return None
    params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return tg("sendMessage", **params)
    except Exception as e:
        log.error(f"שליחת הודעה נכשלה: {e}")
        if not silent_fail:
            raise
        return None

def edit_message(message_id, text, keyboard=None):
    chat_id = STATE.get("chat_id")
    if not (chat_id and message_id):
        return
    params = {"chat_id": chat_id, "message_id": message_id,
              "text": text[:4000], "disable_web_page_preview": True}
    if keyboard is not None:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        tg("editMessageText", **params)
    except Exception as e:
        log.error(f"עריכת הודעה נכשלה: {e}")

def tg_download(file_id: str, dest_dir: Path, fallback_name: str) -> Path:
    info = tg("getFile", file_id=file_id)
    file_path = info.get("file_path")
    if not file_path:
        raise TgError("getFile לא החזיר נתיב")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(file_path).name or fallback_name
    if fallback_name and "." in fallback_name:
        name = fallback_name  # שם מקורי עדיף כשקיים (נושא את שם הלקוח)
    dest = _unique_path(dest_dir / _safe_name(name))
    with requests.get(f"{TG_FILE_API}/{file_path}", stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(64 * 1024):
                fh.write(chunk)
    return dest

# ---------------------------------------------------------------- עזרי קבצים

def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "file"

def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem}({i}){p.suffix}")
        if not cand.exists():
            return cand
    return p.with_name(f"{p.stem}-{uuid.uuid4().hex[:6]}{p.suffix}")

def file_key(p: Path) -> str:
    return f"{p.name}|{p.stat().st_size}"

def to_processed(p: Path, move: bool = False) -> Path:
    """מעתיק (או מעביר) קובץ אל processed/YYYY-MM/ ומחזיר את הנתיב החדש."""
    sub = PROCESSED_DIR / datetime.now().strftime("%Y-%m")
    sub.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(sub / p.name)
    if move:
        shutil.move(str(p), str(dest))
    else:
        shutil.copy2(str(p), str(dest))
    return dest

def to_failed(src, reason: str, text_content: str = ""):
    FAILED_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        if src and Path(src).exists():
            dest = _unique_path(FAILED_DIR / Path(src).name)
            shutil.copy2(str(src), str(dest))
            (FAILED_DIR / f"{dest.stem}.error.txt").write_text(reason, encoding="utf-8")
        else:
            (FAILED_DIR / f"{stamp}.txt").write_text(
                (text_content or "") + "\n\n--- סיבת כשל ---\n" + reason, encoding="utf-8")
    except Exception as e:
        log.error(f"שמירה ב-failed נכשלה: {e}")

def wait_stable(p: Path, tries: int = 12, interval: float = 3.0) -> bool:
    """ממתין שהקובץ יסיים להיכתב/להסתנכרן: גודל זהה בשתי בדיקות עוקבות."""
    last = -1
    for _ in range(tries):
        try:
            size = p.stat().st_size
        except OSError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(interval)
    return False

# ---------------------------------------------------------------- זיהוי לקוח ותאריך

_FILENAME_PREFIXES = ("הקלטת שיחה", "שיחה מוקלטת", "שיחת טלפון", "Call recording",
                      "call recording", "Call_", "Recording", "הקלטה")

def client_from_filename(name: str):
    stem = Path(name).stem
    stem = re.sub("[‎‏‪-‮⁦-⁩]", "", stem)
    for pre in _FILENAME_PREFIXES:
        if stem.startswith(pre):
            stem = stem[len(pre):]
            break
    stem = stem.strip(" -_·")
    stem = re.sub(r"[_\s-]*\d{6,}[_\s\d-]*$", "", stem).strip(" -_·")
    return stem or None

def dt_from_filename(name: str):
    m = re.search(r"(\d{8})[_-](\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    m = re.search(r"(\d{6})[_-](\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S")
        except ValueError:
            pass
    return None

def phone_from_filename(name: str):
    """מחלץ מספר טלפון משם קובץ הקלטה (מקטעי תאריך בני 6 ספרות לא נתפסים)."""
    m = re.search(r"\+?\d{9,15}", name.replace("-", ""))
    return m.group(0) if m else None

_RE_LEGABEI = re.compile(r"^\s*לגבי\s+([^:]{1,60}):\s*", re.S)

def client_from_text(text: str):
    """'לגבי פלוני: ...' — מחזיר (לקוח, הטקסט בלי הפתיח)."""
    m = _RE_LEGABEI.match(text or "")
    if m:
        return m.group(1).strip(), text[m.end():].strip() or text
    return None, text

# ---------------------------------------------------------------- תמלול

class TranscribeError(Exception):
    pass

def _ffmpeg_path():
    p = shutil.which(FFMPEG_BIN)
    if p:
        return p
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    links = local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    if links.exists():
        return str(links)
    packages = local / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():
        for cand in packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            return str(cand)
    return None

def convert_audio(p: Path) -> Path:
    ff = _ffmpeg_path()
    if not ff:
        raise TranscribeError("ffmpeg לא נמצא — נדרש להמרת הפורמט הזה")
    TMP_DIR.mkdir(exist_ok=True)
    out = _unique_path(TMP_DIR / (p.stem + ".mp3"))
    res = subprocess.run([ff, "-y", "-i", str(p), "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)],
                         capture_output=True, timeout=600)
    if res.returncode != 0 or not out.exists():
        raise TranscribeError("המרת אודיו נכשלה (ffmpeg)")
    return out

def transcribe(path: Path) -> str:
    if not GROQ_API_KEY:
        raise TranscribeError("חסר GROQ_API_KEY בקובץ .env")
    work = Path(path)
    converted = None
    if work.suffix.lower() not in GROQ_SUPPORTED or work.stat().st_size > GROQ_MAX_BYTES:
        converted = convert_audio(work)
        work = converted
    try:
        for attempt in (1, 2):
            with open(work, "rb") as fh:
                r = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (work.name, fh)},
                    data={"model": GROQ_MODEL, "language": "he",
                          "response_format": "json", "temperature": "0"},
                    timeout=600)
            if r.status_code == 200:
                text = (r.json().get("text") or "").strip()
                if not text:
                    raise TranscribeError("התמלול חזר ריק")
                return text
            if r.status_code in (400, 413, 422) and attempt == 1 and converted is None:
                converted = convert_audio(Path(path))
                work = converted
                continue
            if r.status_code in (429, 500, 502, 503) and attempt == 1:
                time.sleep(20)
                continue
            raise TranscribeError(f"Groq החזיר {r.status_code}: {r.text[:200]}")
    finally:
        if converted:
            try:
                converted.unlink(missing_ok=True)
            except OSError:
                pass
    raise TranscribeError("התמלול נכשל")

# ---------------------------------------------------------------- חילוץ (claude -p)

class ExtractError(Exception):
    pass

def _claude_path():
    if CLAUDE_BIN:
        return CLAUDE_BIN
    p = shutil.which("claude")
    if p:
        return p
    home = Path.home() / ".local" / "bin" / "claude.exe"
    return str(home) if home.exists() else None

def run_claude(prompt: str, timeout: int = 300) -> str:
    exe = _claude_path()
    if not exe:
        raise ExtractError("claude CLI לא נמצא במחשב")
    res = subprocess.run([exe, "-p", "--output-format", "json"],
                         input=prompt.encode("utf-8"),
                         capture_output=True, timeout=timeout, cwd=str(BASE_DIR))
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace")[:300]
        raise ExtractError(f"claude נכשל (קוד {res.returncode}): {err}")
    try:
        envelope = json.loads(out)
    except Exception:
        return out
    if isinstance(envelope, dict) and envelope.get("type") == "result":
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise ExtractError(f"claude החזיר שגיאה: {envelope.get('subtype') or 'is_error'}")
        result = (envelope.get("result") or "").strip()
        if not result:
            raise ExtractError("claude החזיר פלט ריק")
        return result
    return out

def parse_json_block(text: str):
    """מאתר את בלוק ה-JSON הראשון בטקסט (עמיד לגדרות ``` ולטקסט מסביב)."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            break
        start = text.find("{", start + 1)
    return None

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def normalize_extraction(data: dict, meta: dict) -> dict:
    ext = {}
    ext["client"] = str(data.get("client") or meta.get("client_hint") or "לא משויך").strip() or "לא משויך"
    ext["channel"] = meta["channel"]  # הערוץ נקבע לפי המקור, לא לפי המודל
    # סינון שיחות אישיות: ברירת המחדל עסקית — מדלגים רק על קביעה מפורשת של המודל
    ext["is_business"] = bool(data.get("is_business", True))
    ext["skip_reason"] = str(data.get("skip_reason") or "").strip()
    ext["summary_short"] = str(data.get("summary_short") or "").strip()
    ext["summary_full"] = str(data.get("summary_full") or "").strip()
    ext["decisions"] = [str(d).strip() for d in (data.get("decisions") or []) if str(d).strip()]
    ext["followups"] = [str(d).strip() for d in (data.get("followups") or []) if str(d).strip()]
    ext["open_questions"] = [str(d).strip() for d in (data.get("open_questions") or []) if str(d).strip()]
    tasks = []
    for t in (data.get("tasks") or []):
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        due = str(t.get("due_date") or "").strip()
        priority = str(t.get("priority") or "רגיל").strip()
        if priority not in ("דחוף", "גבוה", "רגיל"):
            priority = "רגיל"
        try:
            cb = int(t.get("callback_minutes"))
            cb = cb if 1 <= cb <= 240 else None
        except (TypeError, ValueError):
            cb = None
        tasks.append({
            "title": title,
            "due_date": due if _DATE_RE.match(due) else None,
            "priority": priority,
            "is_commitment": bool(t.get("is_commitment")),
            "callback_minutes": cb,
            "context": str(t.get("context") or "").strip(),
        })
    ext["tasks"] = tasks
    if not ext["summary_short"]:
        ext["summary_short"] = (ext["summary_full"][:150] or "לא זוהה תוכן ברור")
    return ext

def claude_extract(transcript: str, meta: dict, correction: str = "") -> dict:
    template_path = BASE_DIR / "prompt.md"
    if not template_path.exists():
        raise ExtractError("prompt.md חסר בתיקיית הפרויקט")
    template = template_path.read_text(encoding="utf-8")
    correction_block = ""
    if correction:
        correction_block = ("\n## תיקון מנחי (מחייב — עדכן את החילוץ בהתאם)\n" + correction + "\n")
    prompt = (template
              .replace("{{DATETIME}}", meta.get("dt_str", ""))
              .replace("{{CLIENT_HINT}}", meta.get("client_hint") or "לא ידוע")
              .replace("{{CHANNEL}}", meta.get("channel", ""))
              .replace("{{CONTEXT}}", meta.get("context_hint") or "אין")
              .replace("{{CORRECTION}}", correction_block)
              .replace("{{TRANSCRIPT}}", transcript))
    last_err = "פלט לא תקין"
    for attempt in (1, 2):
        suffix = "" if attempt == 1 else "\n\nחשוב: החזר אך ורק JSON תקין אחד, בלי אף מילה נוספת."
        try:
            out = run_claude(prompt + suffix)
        except subprocess.TimeoutExpired:
            last_err = "חריגת זמן בחילוץ"
            continue
        data = parse_json_block(out)
        # דרישת שדות מהסכמה — מונע קבלת JSON זר (מעטפת, שגיאה) כאילו היה חילוץ
        if isinstance(data, dict) and ({"summary_short", "summary_full", "tasks", "decisions"} & set(data.keys())):
            return normalize_extraction(data, meta)
        last_err = f"לא נמצא JSON תקין בפלט: {out[:150]}"
    raise ExtractError(last_err)

# ---------------------------------------------------------------- מאנדיי

class MondayError(Exception):
    pass

def monday_gql(query: str, variables: dict = None):
    if not MONDAY_API_TOKEN:
        raise MondayError("חסר MONDAY_API_TOKEN בקובץ .env")
    r = requests.post(MONDAY_URL,
                      headers={"Authorization": MONDAY_API_TOKEN,
                               "API-Version": "2024-10",
                               "Content-Type": "application/json"},
                      json={"query": query, "variables": variables or {}},
                      timeout=60)
    try:
        data = r.json()
    except Exception:
        raise MondayError(f"HTTP {r.status_code} ממאנדיי")
    if data.get("errors"):
        raise MondayError("; ".join(e.get("message", "?") for e in data["errors"])[:300])
    if "error_message" in data:
        raise MondayError(data["error_message"])
    return data.get("data") or {}

def board_struct(board_id: str) -> dict:
    """קריאת מבנה חי — תמיד לפני כתיבה (חוק ברזל)."""
    q = "query($ids:[ID!]){boards(ids:$ids){id name url columns{id title type settings_str} groups{id title}}}"
    data = monday_gql(q, {"ids": [str(board_id)]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(f"לוח {board_id} לא נמצא — בדוק את המזהה ב-.env")
    return boards[0]

def status_labels(col: dict) -> list:
    try:
        settings = json.loads(col.get("settings_str") or "{}")
    except Exception:
        return []
    labels = settings.get("labels")
    if isinstance(labels, dict):
        return [v for v in labels.values() if v]
    if isinstance(labels, list):
        return [x.get("label") for x in labels if isinstance(x, dict) and x.get("label")]
    return []

def _pick_col(struct: dict, types: tuple, keywords: tuple):
    cols = [c for c in struct["columns"] if c["type"] in types]
    for kw in keywords:
        for c in cols:
            if kw in (c.get("title") or ""):
                return c
    return None

def build_intake_columns(struct: dict, ext: dict, meta: dict) -> dict:
    cols = {}
    # תאריך — פקודה קפואה: אין פריט בלי תאריך
    col_id = COLUMN_MAP_INTAKE.get("date")
    date_col = None
    if col_id:
        date_col = next((c for c in struct["columns"] if c["id"] == col_id), None)
    if not date_col:
        date_col = _pick_col(struct, ("date",), ("תאריך",)) or \
                   next((c for c in struct["columns"] if c["type"] == "date"), None)
    if date_col:
        val = {"date": meta.get("date_utc") or meta["date"]}
        if meta.get("time_utc") or meta.get("time"):
            val["time"] = meta.get("time_utc") or meta["time"]
        cols[date_col["id"]] = val
    # ערוץ — רק אם קיימת עמודת סטטוס עם תווית תואמת בדיוק (לא יוצרים תוויות)
    ch_col_id = COLUMN_MAP_INTAKE.get("channel")
    for c in struct["columns"]:
        if c["type"] != "status":
            continue
        if (ch_col_id and c["id"] == ch_col_id) or (not ch_col_id and "ערוץ" in (c.get("title") or "")):
            if ext["channel"] in status_labels(c):
                cols[c["id"]] = {"label": ext["channel"]}
            break
    # לקוח / סיכום / מקור — רק אם קיימות עמודות טקסט מתאימות
    for field, kws, value in (
            ("client", ("לקוח", "איש קשר"), ext["client"]),
            ("summary", ("סיכום",), ext["summary_short"]),
            ("source", ("מקור",), meta.get("source_desc", ""))):
        cid = COLUMN_MAP_INTAKE.get(field)
        col = None
        if cid:
            col = next((c for c in struct["columns"] if c["id"] == cid), None)
        if not col:
            col = _pick_col(struct, ("text", "long_text"), kws)
        if col and value:
            if col["type"] == "long_text":
                cols[col["id"]] = {"text": value[:2000]}
            else:
                cols[col["id"]] = value[:255]
    return cols

def build_task_columns(struct: dict, task: dict) -> dict:
    cols = {}
    # תאריך יעד
    cid = COLUMN_MAP_TASKS.get("date")
    date_col = None
    if cid:
        date_col = next((c for c in struct["columns"] if c["id"] == cid), None)
    if not date_col:
        date_col = _pick_col(struct, ("date",), ("יעד", "תאריך")) or \
                   next((c for c in struct["columns"] if c["type"] == "date"), None)
    if date_col:
        # אין תאריך יעד מהשיחה — היום (פקודה קפואה: אין פריט בלי תאריך)
        cols[date_col["id"]] = {"date": task.get("due_date") or date.today().isoformat()}
    # דחיפות
    cid = COLUMN_MAP_TASKS.get("priority")
    pr_col = None
    if cid:
        pr_col = next((c for c in struct["columns"] if c["id"] == cid), None)
    if not pr_col:
        pr_col = _pick_col(struct, ("status",), ("דחיפות", "עדיפות"))
    if pr_col:
        label = PRIORITY_MAP.get(task["priority"], task["priority"])
        if label in status_labels(pr_col):
            cols[pr_col["id"]] = {"label": label}
    # סטטוס התחלתי
    cid = COLUMN_MAP_TASKS.get("status")
    st_col = None
    if cid:
        st_col = next((c for c in struct["columns"] if c["id"] == cid), None)
    if not st_col:
        st_col = _pick_col(struct, ("status",), ("סטטוס",))
    if st_col and st_col is not pr_col and TASK_INITIAL_STATUS in status_labels(st_col):
        cols[st_col["id"]] = {"label": TASK_INITIAL_STATUS}
    return cols

def ensure_group(board_id: str, struct: dict, title: str, create: bool = True) -> str:
    want = (title or "").strip()
    # Match by group ID first (rename-proof) — INTAKE/TASKS group vars hold IDs.
    for g in struct.get("groups") or []:
        if g.get("id") == want:
            return g["id"]
    for g in struct.get("groups") or []:
        if (g.get("title") or "").strip() == want:
            return g["id"]
    if not create:
        raise MondayError(
            f"הקבוצה '{title}' לא קיימת בלוח {struct.get('name', board_id)} — "
            "יצירתה דורשת אישור של נחי (צור אותה במאנדיי או הפעל INTAKE_GROUP_AUTOCREATE=yes)")
    data = monday_gql("mutation($b:ID!,$n:String!){create_group(board_id:$b,group_name:$n){id}}",
                      {"b": str(board_id), "n": title})
    gid = (data.get("create_group") or {}).get("id")
    if not gid:
        raise MondayError(f"יצירת הקבוצה '{title}' נכשלה")
    log.info(f"נוצרה קבוצה '{title}' בלוח {board_id}")
    return gid

def create_item(board_id: str, group_id: str, name: str, colvals: dict) -> str:
    q = ("mutation($b:ID!,$g:String!,$n:String!,$c:JSON)"
         "{create_item(board_id:$b,group_id:$g,item_name:$n,column_values:$c){id}}")
    data = monday_gql(q, {"b": str(board_id), "g": group_id, "n": name[:255],
                          "c": json.dumps(colvals, ensure_ascii=False) if colvals else None})
    item_id = (data.get("create_item") or {}).get("id")
    if not item_id:
        raise MondayError("create_item לא החזיר מזהה")
    return item_id

def create_update(item_id: str, body_html: str):
    q = "mutation($i:ID!,$b:String!){create_update(item_id:$i,body:$b){id}}"
    monday_gql(q, {"i": str(item_id), "b": body_html[:20000]})

# ---------------------------------------------------------------- איתור איש קשר ותיקים

def _norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    return digits[-9:] if len(digits) >= 9 else digits

def _fetch_items_light(board_id: str, col_ids: list, max_pages: int = 4) -> list:
    """שליפה קלה של פריטי לוח (שם + עמודות נבחרות), עד ~800 פריטים."""
    items, cursor = [], None
    for _ in range(max_pages):
        q = ("query($ids:[ID!],$cursor:String,$cols:[String!]){boards(ids:$ids){"
             "items_page(limit:200,cursor:$cursor){cursor items{id name group{id title} "
             "column_values(ids:$cols){id text}}}}}")
        data = monday_gql(q, {"ids": [str(board_id)], "cursor": cursor, "cols": col_ids or []})
        boards = data.get("boards") or []
        page = (boards[0].get("items_page") if boards else None) or {}
        items.extend(page.get("items") or [])
        cursor = page.get("cursor")
        if not cursor:
            break
    return items

def lookup_client(client_name: str, phone: str):
    """מאתר כרטיס איש קשר (טלפון קודם, שם אחריו) ואת התיקים המקושרים אליו.
    כרטיס = אדם, תיק = ישות: לאיש קשר אחד יכולים להיות כמה תיקים.
    כל כשל כאן שקט — העשרה בלבד, לעולם לא חוסם כתיבה."""
    if not LOOKUP_CLIENT:
        return None
    try:
        struct = board_struct(CONTACTS_BOARD_ID)
        phone_cols = [c["id"] for c in struct["columns"]
                      if c["type"] == "phone" or "טלפון" in (c.get("title") or "")]
        contacts = _fetch_items_light(CONTACTS_BOARD_ID, phone_cols)
        target = _norm_phone(phone) if phone else ""
        best = None
        if target:
            for it in contacts:
                for cv in it.get("column_values") or []:
                    if _norm_phone(cv.get("text")) == target:
                        best = it
                        break
                if best:
                    break
        if not best and client_name and client_name != "לא משויך":
            name_n = client_name.strip()
            for it in contacts:
                n = (it.get("name") or "").strip()
                if n and (n == name_n or n in name_n or name_n in n):
                    best = it
                    break
        if not best:
            return None
        contact = {"id": str(best["id"]), "name": best.get("name") or "",
                   "url": f"{struct['url']}/pulses/{best['id']}"}
        contact["files"] = _files_for_contact(contact)
        return contact
    except Exception as e:
        log.warning(f"איתור איש קשר נכשל (לא חוסם): {e}")
        return None

def _files_for_contact(contact: dict) -> list:
    """תיקי הלקוח של איש הקשר — לפי עמודת הקישור לכרטיס, ובגיבוי לפי השם."""
    try:
        struct = board_struct(FILES_BOARD_ID)
        link_cols = [c["id"] for c in struct["columns"]
                     if c["type"] in ("link", "board_relation")
                     or "איש קשר" in (c.get("title") or "")]
        items = _fetch_items_light(FILES_BOARD_ID, link_cols)
        files, seen = [], set()

        def add(it):
            if it["id"] not in seen:
                seen.add(it["id"])
                files.append({"id": str(it["id"]), "name": it.get("name") or "",
                              "url": f"{struct['url']}/pulses/{it['id']}"})

        for it in items:
            for cv in it.get("column_values") or []:
                txt = cv.get("text") or ""
                if txt and (contact["id"] in txt or (contact["name"] and contact["name"] in txt)):
                    add(it)
                    break
        if not files and contact["name"]:
            last = contact["name"].split()[-1]
            if len(last) > 1:
                for it in items:
                    if last in (it.get("name") or ""):
                        add(it)
        return files
    except Exception as e:
        log.warning(f"איתור תיקים נכשל (לא חוסם): {e}")
        return []

# ---------------------------------------------------------------- מניעת כפילויות

def _title_tokens(s: str) -> set:
    s = re.sub(r"[^\w֐-׿ ]", " ", s or "")
    return {w for w in s.split() if len(w) > 1}

def find_duplicate_task(task: dict, client: str, tasks_struct: dict):
    """מחפש משימה פתוחה דומה בלוח: חפיפת מילים גבוהה + אותו תאריך יעד (אם יש).
    מחזיר את הפריט הקיים או None. כשל — None (לא חוסם)."""
    if not DEDUP_TASKS:
        return None
    try:
        date_col = _pick_col(tasks_struct, ("date",), ("יעד", "תאריך"))
        cols = [date_col["id"]] if date_col else []
        items = _fetch_items_light(tasks_struct["id"], cols)
        cand = _title_tokens(task["title"])
        client_toks = _title_tokens(client)
        for it in items:
            g_title = ((it.get("group") or {}).get("title")) or ""
            if "ארכיון" in g_title or "הושלמ" in g_title:
                continue
            toks = _title_tokens(it.get("name"))
            if not toks:
                continue
            overlap = len(toks & cand)
            needed = max(2, int(0.6 * min(len(toks), len(cand))))
            client_match = not client_toks or bool(toks & client_toks)
            if overlap >= needed and client_match:
                it_date = ""
                for cv in it.get("column_values") or []:
                    it_date = cv.get("text") or ""
                if task.get("due_date") and it_date and task["due_date"] not in it_date:
                    continue
                return {"id": str(it["id"]), "name": it.get("name") or "",
                        "url": f"{tasks_struct['url']}/pulses/{it['id']}"}
        return None
    except Exception as e:
        log.warning(f"בדיקת כפילות נכשלה (לא חוסם): {e}")
        return None

def _esc(text: str) -> str:
    return html.escape(str(text or "")).replace("\n", "<br>")

def _ul(items: list) -> str:
    return "<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in items) + "</ul>"

def intake_update_html(ext: dict, meta: dict, task_results: list, duplicates: list = None) -> str:
    parts = []
    if ext["decisions"]:
        parts.append("<b>📝 מה נקבע:</b>" + _ul(ext["decisions"]))
    parts.append("<b>📄 סיכום מלא:</b><br>" + _esc(ext["summary_full"] or ext["summary_short"]))
    if task_results:
        rows = [f"{t['title']}" + (f" — יעד: {t['due']}" if t.get("due") else "") for t in task_results]
        parts.append(f"<b>📌 משימות שנוצרו ({len(task_results)}):</b>" + _ul(rows))
    if duplicates:
        rows = "".join(f"<li><a href=\"{d['url']}\">{_esc(d['existing_name'])}</a></li>"
                       for d in duplicates)
        parts.append(f"<b>♻️ לא נוצרו — קיימות כבר בלוח:</b><ul>{rows}</ul>")
    if ext["followups"]:
        parts.append("<b>🔭 למעקב:</b>" + _ul(ext["followups"]))
    if ext["open_questions"]:
        parts.append("<b>❓ שאלות פתוחות:</b>" + _ul(ext["open_questions"]))
    lookup = meta.get("lookup")
    if lookup:
        parts.append(f"<b>👤 כרטיס איש קשר:</b> <a href=\"{lookup['url']}\">{_esc(lookup['name'])}</a>")
        if lookup.get("files"):
            rows = "".join(f"<li><a href=\"{f['url']}\">{_esc(f['name'])}</a></li>"
                           for f in lookup["files"][:10])
            parts.append(f"<b>🗂️ תיקים של איש הקשר ({len(lookup['files'])}):</b><ul>{rows}</ul>")
    parts.append(f"<b>מקור:</b> {_esc(ext['channel'])} · {_esc(meta.get('source_desc', ''))} · "
                 f"{_esc(meta.get('dt_str', ''))}")
    return "<br><br>".join(parts)

def _is_callback_title(title: str) -> bool:
    # "חזור" תופס אחזור/יחזור/לחזור/תחזור/נחזור; "חזרה" תופס שיחה חוזרת.
    t = title or ""
    return any(k in t for k in ("חזור", "חזרה", "להתקשר", "אתקשר", "שיחה חוזרת", "callback"))


def close_handled_callbacks(ext: dict) -> list:
    """סגירת מעגל: שיחה עם לקוח מזוהה שלא נוצרה בה התחייבות-חזרה חדשה => סוגר
    חזרות פתוחות של אותו לקוח בלוח המשימות, כדי שיירדו מרשימת השיחות של שומר זמן.
    זהיר: מתאים לפי שם הלקוח + כותרת-חזרה בלבד; לעולם לא חוסם את הכתיבה."""
    if cfg("AUTO_CLOSE_CALLBACKS", "1") != "1":
        return []
    client = (ext.get("client") or "").strip()
    if not client or client == "לא משויך":
        return []
    # התחייבות-חזרה חדשה בשיחה => הלולאה נמשכת, לא סוגרים (הקריטריון של נחי).
    # מחמירים לצד הבטוח: כל רמז לחזרה (משימה, callback_minutes או followup) חוסם סגירה.
    has_new_callback = any(
        _is_callback_title(t.get("title", "")) or t.get("callback_minutes") is not None
        for t in ext.get("tasks", [])
    ) or any(_is_callback_title(f) for f in ext.get("followups", []))
    if has_new_callback:
        return []

    closed = []
    try:
        struct = board_struct(TASKS_BOARD_ID)
        cid = COLUMN_MAP_TASKS.get("status")
        st_col = next((c for c in struct["columns"] if c["id"] == cid), None) if cid else None
        if not st_col:
            st_col = _pick_col(struct, ("status",), ("סטטוס",))
        if not st_col:
            return []
        done_label = cfg("CALLBACK_DONE_STATUS", "בוצע")
        if done_label not in status_labels(st_col):
            log.warning(f"סגירת חזרות: אין תווית '{done_label}' בלוח המשימות")
            return []
        pending = [s.strip() for s in cfg("CALLBACK_PENDING_STATUSES", "חדש,בטיפול").split(",") if s.strip()]
        client_toks = _title_tokens(client)
        if not client_toks:
            return []

        for it in _fetch_items_light(TASKS_BOARD_ID, [st_col["id"]]):
            status_txt = next((cv.get("text") or "" for cv in it.get("column_values") or []
                               if cv["id"] == st_col["id"]), "").strip()
            title = it.get("name") or ""
            if status_txt not in pending or not _is_callback_title(title):
                continue
            if not (_title_tokens(title) & client_toks):
                continue
            monday_gql(
                "mutation($b:ID!,$i:ID!,$v:JSON!){change_multiple_column_values("
                "board_id:$b,item_id:$i,column_values:$v){id}}",
                {"b": str(TASKS_BOARD_ID), "i": str(it["id"]),
                 "v": json.dumps({st_col["id"]: {"label": done_label}}, ensure_ascii=False)})
            try:
                create_update(it["id"], f"נסגר אוטומטית: שוחחת עם {_esc(client)} — סוכם ללא חזרה נוספת.")
            except Exception:
                pass
            closed.append({"id": it["id"], "title": title})
            log.info(f"סגירת חזרה אוטומטית: {title} (לקוח {client})")
    except Exception as e:
        log.warning(f"סגירת חזרות נכשלה (לא חוסם): {e}")
    return closed


def attach_summary_to_case(ext: dict, meta: dict):
    """עיקרון בית החולים: ללקוח עם תיק קיים אין צורך לעבור בחדר המיון —
    הסיכום נצמד ישירות לתיק שלו כעדכון. מחזיר {case_id, case_name, case_url}
    או None אם אין תיק / כבוי. לעולם לא חוסם — כשל = חוזר None."""
    if cfg("ROUTE_BY_CASE", "0") != "1":
        return None
    lookup = meta.get("lookup") or {}
    files = lookup.get("files") or []
    if not files:
        return None
    case = files[0]  # התיק המקושר; אם יש כמה — נחי יבחר, כרגע הראשון
    try:
        parts = [f"<b>🎙️ סיכום שיחה — {_esc(meta.get('dt_str',''))}</b>",
                 _esc(ext.get("summary_short", ""))]
        if ext.get("decisions"):
            parts.append("<b>מה נקבע:</b>" + _ul([_esc(d) for d in ext["decisions"]]))
        if ext.get("tasks"):
            rows = [(("🔴 " if t.get("callback_minutes") else "🤝 " if t.get("is_commitment") else "")
                     + _esc(t["title"]) + (f" — יעד {t['due_date']}" if t.get("due_date") else ""))
                    for t in ext["tasks"]]
            parts.append("<b>משימות:</b>" + _ul(rows))
        create_update(case["id"], "<br>".join(parts))
        log.info(f"סיכום נצמד לתיק {case['id']} ({case['name']}) — עקף את חדר המיון")
        return {"case_id": case["id"], "case_name": case["name"], "case_url": case["url"]}
    except Exception as e:
        log.warning(f"הצמדת סיכום לתיק נכשלה (לא חוסם): {e}")
        return None


def monday_write_full(ext: dict, meta: dict) -> dict:
    """כתיבה מלאה: פריט בחדר המיון + משימות בלוח המשימות. מחזיר פירוט למסר האישור.

    רק יצירת פריט הבסיס בחדר המיון יכולה להכשיל את הקריאה (ואז לא נכתב כלום —
    ניסיון חוזר בטוח). כל כשל אחרי שהפריט כבר קיים הופך לאזהרה בתוצאה, כדי
    שלחיצה חוזרת על "אשר" לעולם לא תיצור כפילויות בלוחות החיים.

    עיקרון בית החולים (ROUTE_BY_CASE): ללקוח עם תיק קיים הסיכום נצמד לתיק
    (attach_summary_to_case) — הפריט בחדר המיון עדיין נוצר לתיעוד, אבל התיק
    מקבל את המידע ישירות. כשחדרי המיון יסודרו, נעביר לבַיפַּס מלא.
    """
    case_note = attach_summary_to_case(ext, meta)
    intake = board_struct(INTAKE_BOARD_ID)
    intake_gid = ensure_group(INTAKE_BOARD_ID, intake, INTAKE_GROUP_TITLE,
                              create=INTAKE_GROUP_AUTOCREATE)
    first_line = (ext["summary_short"].splitlines() or [""])[0]
    item_name = f"🎙️ {ext['client']} · {first_line}"[:100]
    intake_cols = build_intake_columns(intake, ext, meta)
    intake_item = create_item(INTAKE_BOARD_ID, intake_gid, item_name, intake_cols)
    intake_url = f"{intake['url']}/pulses/{intake_item}"

    warnings = []
    task_results, task_errors, duplicates = [], [], []
    if ext["tasks"]:
        tasks_struct = None
        try:
            tasks_struct = board_struct(TASKS_BOARD_ID)
            tasks_gid = ensure_group(TASKS_BOARD_ID, tasks_struct, TASKS_GROUP_TITLE)
        except Exception as e:
            log.error(f"לוח המשימות לא זמין: {e}")
            warnings.append("לוח המשימות לא היה זמין — המשימות לא נוצרו")
        if tasks_struct:
            for t in ext["tasks"]:
                try:
                    dup = find_duplicate_task(t, ext["client"], tasks_struct)
                    if dup:
                        duplicates.append({"title": t["title"], "existing_name": dup["name"],
                                           "url": dup["url"]})
                        log.info(f"משימה דומה כבר קיימת ({dup['name']}) — לא נוצרה כפולה")
                        continue
                    name = ("🤝 " if t["is_commitment"] else "") + t["title"]
                    tid = create_item(TASKS_BOARD_ID, tasks_gid, name, build_task_columns(tasks_struct, t))
                    body = (f"<b>לקוח:</b> {_esc(ext['client'])}<br>"
                            f"<b>הקשר:</b> {_esc(t.get('context') or '—')}<br>"
                            + ("<b>🤝 התחייבות של נחי ללקוח</b><br>" if t["is_commitment"] else "")
                            + f"<b>דחיפות:</b> {_esc(t['priority'])}<br>"
                            f"<b>פריט המקור בחדר המיון:</b> <a href=\"{intake_url}\">{_esc(item_name)}</a>")
                    try:
                        create_update(tid, body)
                    except Exception as e:
                        log.error(f"עדכון על משימה {tid} נכשל: {e}")
                    task_results.append({"id": tid, "title": name, "due": t.get("due_date"),
                                         "url": f"{tasks_struct['url']}/pulses/{tid}"})
                except Exception as e:
                    log.error(f"יצירת משימה נכשלה: {t['title']} — {e}")
                    task_errors.append(t["title"])

    try:
        create_update(intake_item, intake_update_html(ext, meta, task_results, duplicates))
    except Exception as e:
        log.error(f"כתיבת העדכון על פריט {intake_item} נכשלה: {e}")
        warnings.append("הפריט נוצר, אבל כתיבת הסיכום המלא עליו נכשלה")
    closed_callbacks = close_handled_callbacks(ext)
    return {"intake_id": intake_item, "intake_name": item_name, "intake_url": intake_url,
            "intake_board": intake["name"], "tasks": task_results,
            "task_errors": task_errors, "warnings": warnings, "duplicates": duplicates,
            "closed_callbacks": closed_callbacks, "case_note": case_note}

# ---------------------------------------------------------------- הודעות אישור

def _duration_str(meta: dict):
    d = meta.get("duration")
    if not d:
        return ""
    try:
        d = int(d)
        return f" ({d // 60}:{d % 60:02d} דק׳)" if d >= 60 else f" ({d} שנ׳)"
    except Exception:
        return ""

def build_summary_message(ext: dict, meta: dict, preview: bool, result: dict = None) -> str:
    lines = []
    if preview:
        lines.append(f"🕓 ממתין לאישורך: {ext['channel']} — {ext['client']}{_duration_str(meta)}")
    else:
        lines.append(f"✅ נקלט: {ext['channel']} — {ext['client']}{_duration_str(meta)}")
    # מקור השיחה מוצג תמיד — גם כשהלקוח לא זוהה מהתוכן
    caller = " · ".join(x for x in (meta.get("client_hint"), meta.get("phone")) if x)
    if caller and (ext["client"] == "לא משויך" or
                   (meta.get("client_hint") and meta["client_hint"] != ext["client"])):
        lines.append(f"📞 מקור השיחה: {caller}")
    lines.append("")
    lines.append(f"📋 {ext['summary_short']}")
    if ext["decisions"]:
        lines.append("")
        lines.append("📝 מה נקבע:")
        lines += [f"• {d}" for d in ext["decisions"]]
    lines.append("")
    if ext["tasks"]:
        if preview:
            lines.append(f"📌 משימות מוצעות ({len(ext['tasks'])}):")
            # 🔴 = הבטחת חזרה בזמן קצוב ("אחזור אליך תוך X דק'") — הכי דחוף.
            src = [{"title": ("🔴 " if t.get("callback_minutes")
                              else "🤝 " if t["is_commitment"] else "") + t["title"],
                    "due": t["due_date"]}
                   for t in ext["tasks"]]
        else:
            created = (result or {}).get("tasks", [])
            lines.append(f"📌 נוצרו {len(created)} משימות:")
            src = created
        for i, t in enumerate(src, 1):
            due = f" — יעד: {t['due']}" if t.get("due") else ""
            lines.append(f"{i}. {t['title']}{due}")
    else:
        lines.append("📌 לא זוהו משימות לביצוע.")
    commits = [t["title"] for t in ext["tasks"] if t["is_commitment"]]
    if commits:
        lines.append(f"🤝 מסומנות כהתחייבות: {len(commits)}")
    lookup = meta.get("lookup")
    if lookup:
        files = lookup.get("files") or []
        if len(files) == 1:
            lines.append(f"👤 {lookup['name']} · תיק: {files[0]['name']}")
        elif files:
            lines.append(f"👤 {lookup['name']} · {len(files)} תיקים: " +
                         ", ".join(f["name"] for f in files[:5]))
        else:
            lines.append(f"👤 כרטיס איש קשר: {lookup['name']} (בלי תיקים מקושרים)")
    if result and result.get("duplicates"):
        for d in result["duplicates"]:
            lines.append(f"♻️ לא נוצרה (כבר קיימת במאנדיי): {d['existing_name']}")
    if result and result.get("case_note"):
        lines.append(f"🗂️ נצמד ישירות לתיק: {result['case_note']['case_name']} (עקף את חדר המיון)")
    if result and result.get("closed_callbacks"):
        for c in result["closed_callbacks"]:
            lines.append(f"☑️ ירדה מרשימת השיחות (טופלה בשיחה זו): {c['title']}")
    warns = []
    if ext["client"] == "לא משויך":
        warns.append("לקוח לא זוהה")
    if any(not t["due_date"] for t in ext["tasks"]):
        warns.append("יש משימות בלי תאריך יעד")
    if result and result.get("task_errors"):
        warns.append(f"משימות שנכשלו ביצירה: {', '.join(result['task_errors'])}")
    if result and result.get("warnings"):
        warns.extend(result["warnings"])
    if warns:
        lines.append("⚠️ " + " | ".join(warns))
    if result:
        lines.append("")
        lines.append(f"🔗 הפריט בחדר המיון: {result['intake_url']}")
    return "\n".join(lines)

# ---------------------------------------------------------------- מצב נודניק

def _wa_link(phone: str, text: str = None):
    """קישור וואטסאפ עם הודעה מוכנה מראש — לחיצה אחת ונשאר רק לשלוח."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    if len(digits) < 11:
        return None
    txt = text or cfg("NUDGE_WA_TEXT",
                      "מתנצל, עדיין לא הספקתי לחזור אליך. אפשר קצת יותר מאוחר? "
                      "אם זה דחוף — אפשר לכתוב לי כאן 🙏")
    return "https://wa.me/" + digits + "?" + urllib.parse.urlencode({"text": txt})

def schedule_nudges(ext: dict, meta: dict):
    """התחייבות עם חלון זמן קצר ("עוד 5 דקות אני חוזר") ← תזכורת נודניק,
    שנדרכת מיד — בלי לחכות לאישור הסיכום."""
    try:
        base = datetime.strptime(meta["date"] + " " + meta["time"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        base = datetime.now()
    max_stale = int(cfg("NUDGE_MAX_STALE_MIN", "30")) * 60
    for t in ext.get("tasks") or []:
        cb = t.get("callback_minutes")
        if not cb:
            continue
        due = base + timedelta(minutes=cb)
        # If the recording was processed long after the promised time (service
        # was down, or the sync arrived late), a "call back in 5 min" reminder
        # is already irrelevant — don't nudge. The task still lives in Monday.
        stale_sec = time.time() - due.timestamp()
        if stale_sec > max_stale:
            log.info(f"נודניק לא נדרך ל{ext['client']} — הבטחת {cb} דק' כבר עברה "
                     f"ב-{int(stale_sec / 60)} דק' (לא רלוונטי). המשימה נשארת במאנדיי.")
            continue
        due_ts = max(due.timestamp(), time.time() + 5)
        nid = uuid.uuid4().hex[:8]
        with STATE_LOCK:
            STATE.setdefault("nudges", {})[nid] = {
                "client": ext["client"], "phone": meta.get("phone"),
                "title": t["title"], "context": t.get("context") or "",
                "topic": (ext.get("summary_short") or "").split("\n")[0][:120],
                "call_time": meta.get("dt_str", ""),
                "window": cb, "due_ts": due_ts,
                "status": "armed", "pings": 0, "created": time.time(),
            }
            save_state()
        log.info(f"נודניק נדרך ({nid}): {ext['client']} תוך {cb} דקות")
        notify(f"⏰ נודניק נדרך: אמרת ל{ext['client']} שתחזור תוך {cb} דקות — אזכיר לך בזמן.")

def nudge_keyboard(nid: str, phone: str):
    kb = [[{"text": "✅ חזרתי — סגור", "callback_data": f"nd|done|{nid}"},
           {"text": "⏰ עוד 10 דק'", "callback_data": f"nd|snooze|{nid}"}]]
    wa = _wa_link(phone)
    if wa:
        kb.append([{"text": "💬 וואטסאפ ללקוח (הודעה מוכנה)", "url": wa}])
    return kb

def process_nudges(now: float):
    with STATE_LOCK:
        nudges = {k: dict(v) for k, v in STATE.get("nudges", {}).items()}
    max_stale = int(cfg("NUDGE_MAX_STALE_MIN", "30")) * 60
    for nid, n in nudges.items():
        if n.get("status") == "done":
            continue
        first = n["status"] == "armed" and now >= n["due_ts"]
        again = (n["status"] == "ringing" and now >= n.get("next_ping", 0))
        if not (first or again):
            continue
        # Stop nudging once we're well past the promised time (service was down,
        # or he simply didn't act) — a late "call back in 5 min" is irrelevant.
        # This caps nagging to ~max_stale after the due time; task stays in Monday.
        if now - n["due_ts"] > max_stale:
            log.info(f"נודניק {nid} ({n['client']}) התיישן — נסגר בשקט. המשימה נשארת במאנדיי.")
            with STATE_LOCK:
                if nid in STATE.get("nudges", {}):
                    STATE["nudges"][nid]["status"] = "done"
                    save_state()
            continue
        if n.get("pings", 0) >= 6:
            notify(f"🔕 מפסיק להציק על {n['client']} — הצקתי 6 פעמים. המשימה נשארת במאנדיי.")
            with STATE_LOCK:
                if nid in STATE.get("nudges", {}):
                    STATE["nudges"][nid]["status"] = "done"
                    save_state()
            continue
        prefix = "🔴 <b>הגיע הזמן לחזור ללקוח!</b>" if first \
            else f"🔴 <b>עדיין מחכה לך (תזכורת {n['pings'] + 1})</b>"
        lines = [prefix, ""]
        lines.append(f"👤 {_tg_esc(n['client'])}")
        phone = n.get("phone")
        if phone:
            lines.append(f"📞 {_tg_esc(phone)}")
        if n.get("title"):
            lines.append(f"📝 {_tg_esc(n['title'])}")
        if n.get("context"):
            lines.append(f"💬 {_tg_esc(n['context'])}")
        if n.get("topic"):
            lines.append(f"🗂️ נושא השיחה: {_tg_esc(n['topic'])}")
        lines.append("")
        promise = f"🔴 <b>הבטחת לחזור תוך {n['window']} דקות" + \
            (f" (בשיחה של {_tg_esc(n['call_time'])})" if n.get("call_time") else "") + "</b>"
        lines.append(promise)
        notify("\n".join(lines), keyboard=nudge_keyboard(nid, phone), parse_mode="HTML")
        with STATE_LOCK:
            live = STATE.get("nudges", {}).get(nid)
            if live:
                live["status"] = "ringing"
                live["pings"] = live.get("pings", 0) + 1
                live["next_ping"] = now + 600
                save_state()

# ---------------------------------------------------------------- שער האישור

def approval_keyboard(pid: str):
    return [[{"text": "✅ אשר ושלח למאנדיי", "callback_data": f"ap|{pid}"},
             {"text": "❌ דחה", "callback_data": f"rj|{pid}"}]]

def create_pending(ext: dict, meta: dict, transcript: str):
    pid = uuid.uuid4().hex[:10]
    msg = notify(build_summary_message(ext, meta, preview=True), keyboard=approval_keyboard(pid))
    with STATE_LOCK:
        STATE["pending"][pid] = {
            "created": time.time(),
            "reminded": False,
            "message_id": msg.get("message_id") if msg else None,
            "extraction": ext,
            "meta": meta,
            "transcript": transcript,
        }
        save_state()
    log.info(f"נוצר פריט ממתין לאישור {pid} — {ext['client']}")

def approve_pending(pid: str, callback_id: str):
    with STATE_LOCK:
        p = STATE["pending"].get(pid)
        correcting = bool(p and p.get("correcting"))
    if correcting:
        try:
            tg("answerCallbackQuery", callback_query_id=callback_id,
               text="תיקון בעיבוד — המתן לתצוגה המעודכנת")
        except Exception:
            pass
        return
    try:
        tg("answerCallbackQuery", callback_query_id=callback_id,
           text="כותב למאנדיי..." if p else "הפריט הזה כבר טופל")
    except Exception:
        pass
    if not p:
        return
    try:
        result = monday_write_full(p["extraction"], p["meta"])
    except Exception as e:
        log.error(f"כתיבה למאנדיי נכשלה (אישור {pid}): {e}")
        notify(f"❌ תקלה בכתיבה למאנדיי: {str(e)[:200]}\nהסיכום עדיין ממתין — אפשר לנסות שוב בכפתורים.",
               reply_to=p.get("message_id"))
        return
    text = "✅ אושר ונכתב למאנדיי\n\n" + build_summary_message(p["extraction"], p["meta"],
                                                              preview=False, result=result)
    edit_message(p.get("message_id"), text, keyboard=[])
    with STATE_LOCK:
        STATE["pending"].pop(pid, None)
        save_state()
    log.info(f"אושר ונכתב {pid}: פריט {result['intake_id']}, {len(result['tasks'])} משימות")

def reject_pending(pid: str, callback_id: str):
    with STATE_LOCK:
        p = STATE["pending"].get(pid)
        if p and p.get("correcting"):
            p = None
            blocked = True
        else:
            blocked = False
            p = STATE["pending"].pop(pid, None)
            save_state()
    if blocked:
        try:
            tg("answerCallbackQuery", callback_query_id=callback_id,
               text="תיקון בעיבוד — המתן לתצוגה המעודכנת")
        except Exception:
            pass
        return
    try:
        tg("answerCallbackQuery", callback_query_id=callback_id,
           text="נדחה" if p else "הפריט הזה כבר טופל")
    except Exception:
        pass
    if not p:
        return
    edit_message(p.get("message_id"), "❌ נדחה — לא נכתב למאנדיי. הקובץ שמור.", keyboard=[])
    log.info(f"נדחה {pid} — {p['extraction'].get('client')}")

def find_pending_by_message(message_id):
    with STATE_LOCK:
        for pid, p in STATE["pending"].items():
            if p.get("message_id") == message_id:
                return pid
    return None

# ---------------------------------------------------------------- צינור העיבוד

work_q = queue.Queue()
IN_FLIGHT = set()
IN_FLIGHT_LOCK = threading.Lock()

def _job_to_json(job: dict) -> dict:
    ser = {k: v for k, v in job.items() if k != "dt"}
    if isinstance(job.get("dt"), datetime):
        ser["dt_iso"] = job["dt"].isoformat()
    return ser

def _job_from_json(ser: dict) -> dict:
    job = dict(ser)
    iso = job.pop("dt_iso", None)
    if iso:
        try:
            job["dt"] = datetime.fromisoformat(iso)
        except ValueError:
            pass
    return job

def enqueue(job: dict):
    """תור + יומן עמיד ב-state.json: עבודה שנקטעה (קריסה/כיבוי) משוחזרת בעלייה.
    עבודות ingest לא נרשמות — סריקת התיקייה ממילא מוצאת אותן מחדש."""
    if job.get("kind") != "ingest" and not job.get("jid"):
        job["jid"] = uuid.uuid4().hex[:12]
        with STATE_LOCK:
            STATE.setdefault("jobs", {})[job["jid"]] = _job_to_json(job)
            save_state()
    work_q.put(job)

def handle_failure(job: dict, err: Exception):
    reason = f"{type(err).__name__}: {err}"
    log.error(f"תקלה בעיבוד {job.get('name', '')}: {reason}\n{traceback.format_exc()}")
    to_failed(job.get("path"), reason, text_content=job.get("text", ""))
    if job.get("kind") == "correction":
        # לא להשאיר את הפריט הממתין נעול על תיקון שנכשל
        with STATE_LOCK:
            p = STATE["pending"].get(job.get("pid"))
            if p:
                p["correcting"] = False
                save_state()
    name = job.get("name") or job.get("kind", "קלט")
    notify(f"❌ תקלה בעיבוד {name}. הקובץ נשמר בצד ולא ילך לאיבוד. סיבה: {str(err)[:150]}")

def process_job(job: dict):
    kind = job["kind"]
    if kind == "correction":
        _process_correction(job)
        return
    dt = job.get("dt") or datetime.now()
    dt_utc = dt.astimezone(timezone.utc)  # מאנדיי מפרש את שדה השעה כ-UTC
    meta = {
        "channel": job["channel"],
        "client_hint": job.get("client_hint"),
        "context_hint": job.get("context_hint"),
        "phone": job.get("phone"),
        "source_desc": job.get("source_desc", ""),
        "duration": job.get("duration"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "date_utc": dt_utc.strftime("%Y-%m-%d"),
        "time_utc": dt_utc.strftime("%H:%M:%S"),
        "dt_str": dt.strftime("%d.%m.%Y %H:%M"),
    }
    if kind == "text":
        transcript = job["text"]
    else:
        transcript = transcribe(Path(job["path"]))
    ext = claude_extract(transcript, meta)
    _finish_extraction(ext, meta, transcript, job)

def _finish_extraction(ext: dict, meta: dict, transcript: str, job: dict):
    # שיחה אישית (משפחה/חברים/זוגיות) — לא נכנסת למערכת: רק הודעה קצרה ותיעוד.
    # ההקלטה עצמה נשמרת ב-processed כרגיל — שום דבר לא נעלם.
    if not ext.get("is_business", True):
        reason = ext.get("skip_reason") or "שיחה אישית"
        log.info(f"שיחה לא-עסקית דולגה ({reason}): {job.get('name', '')}")
        notify(f"🔕 {ext['client']} — זוהתה שיחה אישית, לא נכנסה למאנדיי.")
        p = job.get("path")
        if p and job.get("move_when_done"):
            try:
                to_processed(Path(p), move=True)
            except Exception as e:
                log.error(f"העברה ל-processed נכשלה: {e}")
        return
    # נודניק נדרך מיד — התחייבות של דקות לא מחכה לאישור הסיכום
    schedule_nudges(ext, meta)
    # העשרה: כרטיס איש קשר + תיקים — לפני השער, כדי שיופיעו כבר בתצוגה המקדימה
    if "lookup" not in meta:
        meta["lookup"] = lookup_client(ext["client"], meta.get("phone"))
    if ext["channel"] in APPROVAL_CHANNELS:
        create_pending(ext, meta, transcript)
    else:
        result = monday_write_full(ext, meta)
        notify(build_summary_message(ext, meta, preview=False, result=result))
        log.info(f"נכתב למאנדיי: פריט {result['intake_id']}, {len(result['tasks'])} משימות")
    # תיעוד סיום: קובץ שהורד מטלגרם עובר ל-processed (קובץ מהתיקייה כבר הועתק בקליטה)
    p = job.get("path")
    if p and job.get("move_when_done"):
        try:
            to_processed(Path(p), move=True)
        except Exception as e:
            log.error(f"העברה ל-processed נכשלה: {e}")

def _process_correction(job: dict):
    pid = job["pid"]
    with STATE_LOCK:
        p = STATE["pending"].get(pid)
    if not p:
        notify("הפריט שניסית לתקן כבר טופל.")
        return
    try:
        ext = claude_extract(p["transcript"], p["meta"], correction=job["text"])
    except Exception as e:
        # תיקון נכשל — משחררים את הנעילה ומחזירים את הכפתורים המקוריים
        with STATE_LOCK:
            p2 = STATE["pending"].get(pid)
            if p2:
                p2["correcting"] = False
                save_state()
        if p.get("message_id"):
            edit_message(p["message_id"],
                         build_summary_message(p["extraction"], p["meta"], preview=True),
                         keyboard=approval_keyboard(pid))
        notify(f"❌ התיקון נכשל ({str(e)[:120]}). הסיכום המקורי עדיין ממתין לאישורך.")
        return
    new_msg = notify(build_summary_message(ext, p["meta"], preview=True),
                     keyboard=approval_keyboard(pid))
    with STATE_LOCK:
        p2 = STATE["pending"].get(pid)
        if p2 is None:
            notify("הפריט טופל בינתיים — התיקון לא הוחל.")
            return
        old_mid = p2.get("message_id")
        p2["extraction"] = ext
        p2["correcting"] = False
        p2["reminded"] = False
        p2["created"] = time.time()
        # אם שליחת התצוגה נכשלה — משאירים message_id ריק והתחזוקה תשלח שוב
        p2["message_id"] = new_msg.get("message_id") if new_msg else None
        save_state()
    if new_msg and old_mid and new_msg.get("message_id") != old_mid:
        edit_message(old_mid, "🔁 הוחלף בתצוגה מעודכנת (למטה).", keyboard=[])
    log.info(f"עודכן פריט ממתין {pid} לפי תיקון של נחי")

def dispatch_job(job: dict):
    if job.get("kind") == "ingest":
        ingest_inbox_file(Path(job["path"]))
    else:
        process_job(job)

def worker_loop():
    while True:
        job = work_q.get()
        try:
            dispatch_job(job)
        except Exception as e:
            try:
                handle_failure(job, e)
            except Exception:
                log.error("גם הטיפול בכשל נכשל:\n" + traceback.format_exc())
        finally:
            # סימון "בוצע" רק בסיום מלא (הצלחה או רישום מסודר ב-failed) —
            # קריסה באמצע משאירה את הקובץ/העבודה לשחזור בעלייה הבאה
            with STATE_LOCK:
                if job.get("done_key"):
                    STATE["processed_files"][job["done_key"]] = \
                        datetime.now().isoformat(timespec="seconds")
                if job.get("jid"):
                    STATE.get("jobs", {}).pop(job["jid"], None)
                if job.get("done_key") or job.get("jid"):
                    save_state()
            if job.get("inflight_key"):
                with IN_FLIGHT_LOCK:
                    IN_FLIGHT.discard(job["inflight_key"])
            work_q.task_done()

# ---------------------------------------------------------------- מאזין א' — טלגרם

def _learn_chat(msg: dict):
    if STATE.get("chat_id"):
        return
    with STATE_LOCK:
        STATE["chat_id"] = msg["chat"]["id"]
        save_state()
    notify("הסוכן חי ✅\nהצ'אט הזה נקבע כערוץ הקבוע בינינו. כל מה שתשלח לכאן — "
           "טקסט, הקלטה קולית או קובץ אודיו — יסתכם וייכנס למאנדיי.")
    log.info(f"נלמד chat_id: {STATE['chat_id']}")

def handle_telegram_message(msg: dict, update_id=None):
    from_id = str((msg.get("from") or {}).get("id", ""))
    if from_id != str(NAHI_TELEGRAM_ID):
        log.warning(f"הודעה ממשתמש לא מורשה {from_id} — התעלמות")
        return
    _learn_chat(msg)
    # עדכון שכבר נרשם ביומן העבודות (מסירה חוזרת אחרי קריסה) — לא מעבדים שוב
    if update_id is not None:
        with STATE_LOCK:
            if any(j.get("update_id") == update_id for j in STATE.get("jobs", {}).values()):
                return
    text = msg.get("text") or ""
    dt = datetime.fromtimestamp(msg.get("date", time.time()))

    if text.strip() == "/start":
        notify("הסוכן חי ✅ אפשר לשלוח טקסט, הקלטה קולית או קובץ אודיו.")
        return

    # תיקון בתגובה (Reply) לתצוגה מקדימה ממתינה
    reply = msg.get("reply_to_message")
    if reply and text.strip():
        pid = find_pending_by_message(reply.get("message_id"))
        if pid:
            with STATE_LOCK:
                p = STATE["pending"].get(pid)
                if p and p.get("correcting"):
                    notify("⏳ תיקון קודם עדיין בעיבוד — שלח שוב כשהתצוגה תתעדכן.")
                    return
                if p:
                    p["correcting"] = True
                    save_state()
            # נועלים את הכפתורים הישנים כדי שאי אפשר יהיה לאשר גרסה לא מתוקנת
            edit_message(reply.get("message_id"),
                         "🔄 מעבד תיקון... התצוגה המעודכנת תגיע עוד רגע.", keyboard=[])
            enqueue({"kind": "correction", "pid": pid, "text": text.strip(),
                     "name": "תיקון", "update_id": update_id})
            return

    if text.strip():
        client, body = client_from_text(text)
        enqueue({"kind": "text", "text": text.strip(), "channel": "הודעה",
                 "client_hint": client, "dt": dt, "name": "הודעת טקסט",
                 "source_desc": "טלגרם · הודעת טקסט", "update_id": update_id})
        return

    media, channel, desc = None, None, ""
    caption = (msg.get("caption") or "").strip()
    if msg.get("voice"):
        media, channel = msg["voice"], "תזכורת"
        desc = "טלגרם · הקלטה קולית"
    elif msg.get("audio"):
        media, channel = msg["audio"], "שיתוף"
        desc = "טלגרם · קובץ אודיו"
    elif msg.get("document"):
        doc = msg["document"]
        if (doc.get("mime_type") or "").startswith("audio/") or \
                Path(doc.get("file_name") or "").suffix.lower() in AUDIO_EXTS:
            media, channel = doc, "שיתוף"
            desc = "טלגרם · קובץ משותף"
        else:
            notify("⚠️ קיבלתי קובץ שאינו אודיו — כרגע אני מטפל רק בטקסט ובהקלטות.")
            return
    if not media:
        # תמונה / וידאו / מדבקה / איש קשר וכד' — לא נעלמים בשקט
        log.warning(f"הודעת טלגרם מסוג לא נתמך (message_id={msg.get('message_id')}, "
                    f"שדות: {sorted(msg.keys())})")
        notify("⚠️ קיבלתי הודעה מסוג שאני עוד לא יודע לעבד (תמונה/וידאו/אחר). "
               "אני מטפל בטקסט, הקלטות קוליות וקבצי אודיו. התכוונת להקליט? שלח הקלטה קולית.")
        return

    size = media.get("file_size") or 0
    fallback = media.get("file_name") or f"tg-{msg.get('message_id')}.oga"
    if size > TG_MAX_DOWNLOAD:
        notify("⚠️ הקובץ גדול מדי לערוץ הזה — שים אותו בתיקיית הסנכרון ואטפל בו משם.")
        to_failed(None, f"קובץ טלגרם גדול מ-20MB: {fallback} ({size} בתים)")
        return

    client_hint = None
    context_hint = None
    if caption:
        c, _ = client_from_text(caption)
        if c:
            client_hint = c
        elif len(caption) <= 40 and "\n" not in caption:
            client_hint = caption
        else:
            context_hint = caption
    if not client_hint and media.get("file_name"):
        client_hint = client_from_filename(media["file_name"])

    notify(f"🎧 נקלט ({desc.split('·')[-1].strip()}). מתמלל ומסכם...")
    try:
        TMP_DIR.mkdir(exist_ok=True)
        local = tg_download(media["file_id"], TMP_DIR, fallback)
    except Exception as e:
        if "too big" in str(e).lower():
            notify("⚠️ הקובץ גדול מדי לערוץ הזה — שים אותו בתיקיית הסנכרון ואטפל בו משם.")
            to_failed(None, f"קובץ טלגרם גדול מדי: {fallback}")
        else:
            notify(f"❌ הורדת הקובץ מטלגרם נכשלה: {str(e)[:120]}")
            to_failed(None, f"הורדת {fallback} נכשלה: {e}")
        return
    enqueue({"kind": "audio", "path": str(local), "channel": channel,
             "client_hint": client_hint, "context_hint": context_hint,
             "duration": media.get("duration"), "dt": dt, "name": local.name,
             "source_desc": desc, "move_when_done": True, "update_id": update_id})

def handle_callback(cb: dict):
    from_id = str((cb.get("from") or {}).get("id", ""))
    cb_id = cb.get("id")
    if from_id != str(NAHI_TELEGRAM_ID):
        log.warning(f"callback ממשתמש לא מורשה {from_id}")
        try:
            tg("answerCallbackQuery", callback_query_id=cb_id)
        except Exception:
            pass
        return
    data = cb.get("data") or ""
    if data.startswith("nd|"):
        _, action, nid = data.split("|", 2)
        client = ""
        with STATE_LOCK:
            n = STATE.get("nudges", {}).get(nid)
            if n:
                client = n.get("client", "")
                if action == "done":
                    n["status"] = "done"
                elif action == "snooze":
                    n["status"] = "ringing"
                    n["next_ping"] = time.time() + 600
                    n["pings"] = max(0, n.get("pings", 1) - 1)  # דחייה יזומה לא נספרת כהצקה
                save_state()
        try:
            tg("answerCallbackQuery", callback_query_id=cb_id,
               text="סגור, כל הכבוד ✅" if action == "done" else "אזכיר בעוד 10 דקות ⏰")
        except Exception:
            pass
        # משוב שרואים: ההודעה עצמה מתחלפת, לא רק בועה חולפת
        mid = (cb.get("message") or {}).get("message_id")
        if mid and n:
            if action == "done":
                edit_message(mid, f"✅ טופל — חזרת ל{client}. הנודניק כובה.", keyboard=[])
            else:
                edit_message(mid, f"⏰ נדחה — אזכיר שוב בעוד 10 דקות ({client}).", keyboard=[])
        return
    if "|" not in data:
        try:
            tg("answerCallbackQuery", callback_query_id=cb_id)
        except Exception:
            pass
        return
    action, pid = data.split("|", 1)
    if action == "ap":
        approve_pending(pid, cb_id)
    elif action == "rj":
        reject_pending(pid, cb_id)

def telegram_loop():
    backoff = 5
    while True:
        try:
            updates = tg("getUpdates", http_timeout=75,
                         offset=STATE.get("offset", 0) + 1,
                         timeout=50, allowed_updates=["message", "callback_query"])
            backoff = 5
        except Exception as e:
            log.error(f"getUpdates נכשל: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue
        for u in updates or []:
            try:
                if u.get("message"):
                    handle_telegram_message(u["message"], u.get("update_id"))
                elif u.get("callback_query"):
                    handle_callback(u["callback_query"])
            except Exception:
                log.error("שגיאה בטיפול בעדכון טלגרם:\n" + traceback.format_exc())
            finally:
                # ה-offset מתקדם רק אחרי שהעבודה נרשמה ביומן העמיד —
                # קריסה באמצע לא מאבדת הודעה (במקרה גרוע: עיבוד כפול, לא אובדן)
                with STATE_LOCK:
                    STATE["offset"] = max(STATE.get("offset", 0), u.get("update_id", 0))
        if updates:
            save_state()

# ---------------------------------------------------------------- מאזין ב' — תיקיית הקלטות

def ingest_inbox_file(path: Path):
    """קליטת קובץ מתיקיית הסנכרון: קריאה בלבד מהמקור — העתקה ל-processed ועיבוד משם.
    הרישום כ"בוצע" נעשה רק בסיום העיבוד (בתור העבודה) — לא כאן."""
    if not wait_stable(path):
        log.warning(f"הקובץ לא התייצב — מדלג בינתיים: {path.name}")
        return
    key = file_key(path)
    with STATE_LOCK:
        if key in STATE["processed_files"]:
            return
        if any(j.get("done_key") == key for j in STATE.get("jobs", {}).values()):
            return  # כבר בעיבוד/ביומן העבודות
    if INBOX_MIN_DATE:
        f_dt = dt_from_filename(path.name) or datetime.fromtimestamp(path.stat().st_mtime)
        if f_dt.strftime("%Y-%m-%d") < INBOX_MIN_DATE:
            with STATE_LOCK:
                STATE["processed_files"][key] = "דולג-ישן " + datetime.now().isoformat(timespec="seconds")
                save_state()
            log.info(f"הקלטה ישנה (לפני {INBOX_MIN_DATE}) — נרשמה ודולגה: {path.name}")
            return
    copy = to_processed(path, move=False)
    client = client_from_filename(path.name)
    dt = dt_from_filename(path.name) or datetime.fromtimestamp(path.stat().st_mtime)
    notify(f"🎧 נקלטה הקלטת שיחה{' — ' + client if client else ''}. מתמלל ומסכם...")
    enqueue({"kind": "audio", "path": str(copy), "channel": "שיחה",
             "client_hint": client, "phone": phone_from_filename(path.name),
             "dt": dt, "name": path.name,
             "source_desc": f"הקלטת שיחה · {path.name}", "done_key": key})

def maybe_enqueue_inbox(path: Path):
    if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
        return
    ikey = f"inbox::{path.name}"
    with IN_FLIGHT_LOCK:
        if ikey in IN_FLIGHT:
            return
        IN_FLIGHT.add(ikey)
    enqueue({"kind": "ingest", "path": str(path), "name": path.name, "inflight_key": ikey})

def rescan_inbox():
    if not INBOX_PATH.exists():
        return
    try:
        for p in sorted(INBOX_PATH.iterdir()):
            if p.suffix.lower() in AUDIO_EXTS and p.is_file():
                key_exists = False
                try:
                    key_exists = file_key(p) in STATE["processed_files"]
                except OSError:
                    continue
                if not key_exists:
                    maybe_enqueue_inbox(p)
    except Exception as e:
        log.error(f"סריקת תיקיית הקליטה נכשלה: {e}")

if WATCHDOG_OK:
    class InboxHandler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                maybe_enqueue_inbox(Path(event.src_path))

        def on_moved(self, event):
            if not event.is_directory:
                maybe_enqueue_inbox(Path(event.dest_path))

def start_inbox_watcher():
    if not INBOX_PATH.exists():
        try:
            INBOX_PATH.mkdir(parents=True, exist_ok=True)
        except Exception:
            log.error(f"תיקיית הקליטה לא קיימת ולא ניתנת ליצירה: {INBOX_PATH}")
            return None
    if not WATCHDOG_OK:
        log.warning("watchdog לא מותקן — נסמך על סריקה מחזורית בלבד")
        return None
    observer = Observer()
    observer.schedule(InboxHandler(), str(INBOX_PATH), recursive=False)
    observer.daemon = True
    observer.start()
    log.info(f"מאזין על תיקיית הקליטה: {INBOX_PATH}")
    return observer

# ---------------------------------------------------------------- תחזוקה מחזורית

def housekeeping_loop():
    last_rescan = 0.0
    while True:
        time.sleep(60)
        try:
            with STATE_LOCK:
                STATE["last_alive"] = time.time()
                pending = dict(STATE["pending"])
                save_state()
            now = time.time()
            for pid, p in pending.items():
                # תצוגה מקדימה שלא נשלחה (טלגרם נפל / עוד לא נלמד chat_id) — שולחים שוב
                if p.get("message_id") is None and STATE.get("chat_id") and not p.get("correcting"):
                    msg = notify(build_summary_message(p["extraction"], p["meta"], preview=True),
                                 keyboard=approval_keyboard(pid))
                    if msg:
                        with STATE_LOCK:
                            if pid in STATE["pending"]:
                                STATE["pending"][pid]["message_id"] = msg.get("message_id")
                                save_state()
                    continue
                if not p.get("reminded") and now - p.get("created", now) > REMINDER_HOURS * 3600:
                    client = (p.get("extraction") or {}).get("client", "")
                    notify(f"⏰ תזכורת: סיכום של {client} עדיין ממתין לאישורך.",
                           reply_to=p.get("message_id"))
                    with STATE_LOCK:
                        if pid in STATE["pending"]:
                            STATE["pending"][pid]["reminded"] = True
                            save_state()
            process_nudges(now)
            if now - last_rescan > RESCAN_SECONDS:
                last_rescan = now
                rescan_inbox()
        except Exception:
            log.error("שגיאה בתחזוקה מחזורית:\n" + traceback.format_exc())

# ---------------------------------------------------------------- נעילה

_LOCK_FH = None

def acquire_lock():
    """נעילת קובץ ברמת מערכת ההפעלה — אטומית, ומשתחררת אוטומטית גם בקריסה
    (אין נעילות "רפאים" אחרי ריסטארט, ואין תלות ב-PID שעלול למוחזר)."""
    global _LOCK_FH
    _LOCK_FH = open(LOCK_PATH, "a+")
    try:
        _LOCK_FH.seek(0)
        msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("הסוכן כבר רץ. אין צורך בהפעלה כפולה.")
        sys.exit(0)
    _LOCK_FH.seek(0)
    _LOCK_FH.truncate()
    _LOCK_FH.write(str(os.getpid()))
    _LOCK_FH.flush()

    def _release():
        try:
            _LOCK_FH.seek(0)
            msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_UNLCK, 1)
            _LOCK_FH.close()
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    atexit.register(_release)

# ---------------------------------------------------------------- בדיקת סביבה

def check_env() -> int:
    print(f"בדיקת סביבה — סוכן יומן v{VERSION}")
    print("-" * 40)
    problems = 0

    def ok(label, detail=""):
        print(f"✅ {label}" + (f" — {detail}" if detail else ""))

    def bad(label, detail=""):
        nonlocal problems
        problems += 1
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))

    for key, val in (("BOT_TOKEN", BOT_TOKEN), ("NAHI_TELEGRAM_ID", NAHI_TELEGRAM_ID),
                     ("GROQ_API_KEY", GROQ_API_KEY), ("MONDAY_API_TOKEN", MONDAY_API_TOKEN)):
        ok(f".env: {key}") if val else bad(f".env: {key}", "חסר")

    ff = _ffmpeg_path()
    ok("ffmpeg", ff) if ff else bad("ffmpeg", "לא נמצא")
    cl = _claude_path()
    ok("claude CLI", cl) if cl else bad("claude CLI", "לא נמצא")
    ok("watchdog") if WATCHDOG_OK else bad("watchdog", "לא מותקן — py -m pip install watchdog")

    if BOT_TOKEN:
        try:
            me = tg("getMe", http_timeout=15)
            ok("טלגרם", f"מחובר כ-@{me.get('username')}")
        except Exception as e:
            bad("טלגרם", str(e)[:120])
    if GROQ_API_KEY:
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=15)
            ok("Groq", "מפתח תקין") if r.status_code == 200 else bad("Groq", f"HTTP {r.status_code}")
        except Exception as e:
            bad("Groq", str(e)[:120])
    if MONDAY_API_TOKEN:
        try:
            data = monday_gql("query{me{name}}")
            ok("מאנדיי", f"מחובר כ-{(data.get('me') or {}).get('name')}")
            for label, bid in (("לוח חדר המיון", INTAKE_BOARD_ID), ("לוח המשימות", TASKS_BOARD_ID)):
                try:
                    b = board_struct(bid)
                    ok(label, f"{b['name']} ({bid})")
                except Exception as e:
                    bad(label, str(e)[:120])
        except Exception as e:
            bad("מאנדיי", str(e)[:120])

    if INBOX_PATH.exists():
        ok("תיקיית קליטה", str(INBOX_PATH))
    else:
        print(f"⚠️ תיקיית קליטה עדיין לא קיימת: {INBOX_PATH} (תיווצר בהפעלה)")

    print("-" * 40)
    print("הכול תקין ✅" if problems == 0 else f"נמצאו {problems} בעיות — ראה למעלה")
    return 0 if problems == 0 else 1

# ---------------------------------------------------------------- הפעלה

def restore_jobs():
    """שחזור עבודות שנקטעו באמצע (קריסה/כיבוי) מיומן העבודות שב-state.json."""
    with STATE_LOCK:
        # דגלי "מעבד תיקון" שנשארו תלויים בלי עבודת תיקון חיה — משוחררים
        live_correction_pids = {j.get("pid") for j in STATE.get("jobs", {}).values()
                                if j.get("kind") == "correction"}
        for pid, p in STATE.get("pending", {}).items():
            if p.get("correcting") and pid not in live_correction_pids:
                p["correcting"] = False
        leftover = dict(STATE.get("jobs", {}))
        if leftover:
            save_state()
    restored = 0
    for jid, ser in leftover.items():
        job = _job_from_json(ser)
        job["jid"] = jid
        path = job.get("path")
        if job.get("kind") == "audio" and (not path or not Path(path).exists()):
            to_failed(None, f"שחזור אחרי הפסקה: הקובץ של {job.get('name', '?')} לא נמצא")
            notify(f"❌ הקלט {job.get('name', '?')} אבד בהפסקת השירות — שלח אותו שוב.")
            with STATE_LOCK:
                STATE.get("jobs", {}).pop(jid, None)
                save_state()
            continue
        work_q.put(job)  # ישירות לתור — רשומת היומן הקיימת נשארת עד סיום
        restored += 1
    if restored:
        log.info(f"שוחזרו {restored} עבודות שנקטעו בהפעלה הקודמת")
        notify(f"🔄 ממשיך לעבד {restored} קלטים שנקטעו בהפסקה האחרונה.")

def startup_notifications():
    gap_msg = None
    last = STATE.get("last_alive")
    if last:
        gap_hours = (time.time() - last) / 3600
        if gap_hours > 20:
            gap_msg = (f"הייתי כבוי {int(gap_hours)} שעות. "
                       "אם שלחת משהו לפני יותר מיממה — שלח אותו שוב.")
    if gap_msg:
        notify(gap_msg)
    pending = STATE.get("pending") or {}
    if pending:
        names = [((p.get("extraction") or {}).get("client") or "?") for p in pending.values()]
        notify(f"⏳ ממתינים לאישורך: {len(pending)} סיכומים — " + ", ".join(names[:10]))

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    if "--version" in args:
        print(f"סוכן יומן v{VERSION}")
        return 0
    if "--check" in args:
        load_state()
        return check_env()

    missing = [k for k, v in (("BOT_TOKEN", BOT_TOKEN), ("NAHI_TELEGRAM_ID", NAHI_TELEGRAM_ID),
                              ("GROQ_API_KEY", GROQ_API_KEY), ("MONDAY_API_TOKEN", MONDAY_API_TOKEN)) if not v]
    if missing:
        print("חסרים ערכים בקובץ .env: " + ", ".join(missing))
        print("העתק את .env.example ל-.env ומלא את הערכים, ואז הפעל שוב.")
        return 1

    acquire_lock()
    load_state()
    log.info(f"סוכן יומן v{VERSION} עולה. תיקיית קליטה: {INBOX_PATH}")

    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    threading.Thread(target=housekeeping_loop, daemon=True, name="housekeeping").start()
    start_inbox_watcher()

    startup_notifications()
    restore_jobs()
    rescan_inbox()

    try:
        telegram_loop()
    except KeyboardInterrupt:
        log.info("נעצר ידנית.")
    finally:
        with STATE_LOCK:
            STATE["last_alive"] = time.time()
            save_state()
    return 0

if __name__ == "__main__":
    sys.exit(main())
