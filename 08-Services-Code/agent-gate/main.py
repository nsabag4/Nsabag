"""Agent Gate: one Telegram bot per Sabag agent.

Question in Telegram (text/voice/photo) -> local Claude CLI with the agent's
role prompt and free tool access -> answer back as text, plus a voice note for
short answers. Locked to a single allowed chat id.
"""
import json
import logging
import os
import subprocess
import tempfile
import threading
import time

import requests
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE, "inbox"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "agent-gate.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("gate")

ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "").strip() or "claude"
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "").strip() or "ffmpeg"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
CLAUDE_TIMEOUT_SEC = int(os.getenv("CLAUDE_TIMEOUT_SEC", "300"))
VOICE_REPLIES = os.getenv("VOICE_REPLIES", "1") == "1"
VOICE_MAX_CHARS = int(os.getenv("VOICE_MAX_CHARS", "800"))


# ---------- Telegram helpers ----------

def tg(token, method, timeout=30, **kwargs):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=kwargs, timeout=timeout)
    return r.json()


def tg_download(token, file_id):
    info = requests.get(
        f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=30
    ).json()
    path = info["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=60).content


def send_text(token, chat_id, text):
    for i in range(0, len(text), 4000):
        tg(token, "sendMessage", chat_id=chat_id, text=text[i:i + 4000])


# ---------- voice in (Groq whisper) / voice out (gTTS + ffmpeg) ----------

def transcribe(ogg_bytes):
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("q.ogg", ogg_bytes, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "he"},
            timeout=120,
        )
        return (r.json().get("text") or "").strip() or None
    except Exception:
        log.exception("transcribe failed")
        return None


def make_voice(text):
    """Hebrew TTS -> opus voice file path, or None on any failure."""
    try:
        from gtts import gTTS
        mp3 = tempfile.mktemp(suffix=".mp3")
        ogg = tempfile.mktemp(suffix=".ogg")
        gTTS(text=text, lang="iw").save(mp3)
        p = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "32k", ogg],
            capture_output=True, timeout=60,
        )
        os.unlink(mp3)
        if p.returncode == 0 and os.path.exists(ogg):
            return ogg
    except Exception:
        log.exception("make_voice failed")
    return None


def send_voice(token, chat_id, text):
    ogg = make_voice(text)
    if not ogg:
        return
    try:
        with open(ogg, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendVoice",
                data={"chat_id": chat_id},
                files={"voice": ("a.ogg", f, "audio/ogg")},
                timeout=60,
            )
    except Exception:
        log.exception("send_voice failed")
    finally:
        try:
            os.unlink(ogg)
        except OSError:
            pass


# ---------- the agent ----------

class Agent:
    def __init__(self, key, cfg):
        self.key = key
        self.name = cfg["name"]
        self.token = os.getenv(cfg["token_env"], "").strip()
        self.cwd = cfg["cwd"]
        self.prompt_path = os.path.join(BASE, "prompts", cfg["prompt"])
        self.history = []  # [(question, answer)] most recent last

    def role_prompt(self):
        with open(self.prompt_path, encoding="utf-8") as f:
            return f.read()

    def ask(self, question):
        parts = [self.role_prompt()]
        if self.history:
            parts.append("השיחה האחרונה (להקשר בלבד):")
            for q, a in self.history[-3:]:
                parts.append(f"נחי שאל: {q}\nענית: {a[:500]}")
        parts.append(f"השאלה החדשה של נחי:\n{question}")
        parts.append("ענה בעברית, קצר וממוקד. עד 3 נקודות. אם שלפת נתון — צרף מזהה או מקור.")
        prompt = "\n\n".join(parts)

        env = dict(os.environ)
        for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(var, None)

        log.info("%s: spawning claude (cwd=%s, prompt %d chars)", self.key, self.cwd, len(prompt))
        t0 = time.time()
        out = subprocess.run(
            [CLAUDE_BIN, "-p", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.cwd,
            env=env,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
        log.info("%s: claude done rc=%s in %.0fs", self.key, out.returncode, time.time() - t0)
        answer = (out.stdout or "").strip()
        if not answer:
            raise RuntimeError(f"empty answer, stderr: {(out.stderr or '')[:300]}")
        self.history.append((question, answer))
        self.history = self.history[-6:]
        return answer


def typing_loop(agent, chat_id, stop_event):
    while not stop_event.wait(6):
        try:
            tg(agent.token, "sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass


def handle_message(agent, msg):
    chat_id = msg["chat"]["id"]
    if chat_id != ALLOWED_CHAT_ID:
        log.warning("%s: ignored message from foreign chat %s", agent.key, chat_id)
        return

    question = (msg.get("text") or "").strip()
    prefix = ""

    if msg.get("voice"):
        heard = transcribe(tg_download(agent.token, msg["voice"]["file_id"]))
        if not heard:
            send_text(agent.token, chat_id, "לא הצלחתי לתמלל את ההקלטה. נסה שוב או כתוב לי.")
            return
        question = heard
        prefix = f"🎙️ שמעתי: {heard}\n\n"

    if msg.get("photo"):
        img_path = os.path.join(BASE, "inbox", f"photo_{msg['message_id']}.jpg")
        with open(img_path, "wb") as f:
            f.write(tg_download(agent.token, msg["photo"][-1]["file_id"]))
        caption = (msg.get("caption") or "").strip()
        question = (caption or "נחי שלח תמונה. תסתכל בה וענה בהתאם.") + f"\n[תמונה מצורפת נשמרה בנתיב: {img_path} — פתח אותה עם כלי הקריאה]"

    if not question:
        log.info("%s <- (message with no text/voice/photo — ignored)", agent.key)
        return
    if question in ("/start", "start"):
        log.info("%s <- /start (greeting sent)", agent.key)
        send_text(agent.token, chat_id, f"כאן {agent.name}. שאל אותי הכול — טקסט או הקלטה.")
        return

    log.info("%s <- %.120s", agent.key, question)
    stop = threading.Event()
    t = threading.Thread(target=typing_loop, args=(agent, chat_id, stop), daemon=True)
    t.start()
    try:
        answer = agent.ask(question)
    except subprocess.TimeoutExpired:
        answer = "הבדיקה לקחה יותר מדי זמן והופסקה. נסה לשאול ממוקד יותר."
    except Exception:
        log.exception("%s: ask failed", agent.key)
        answer = "משהו השתבש אצלי. נסה שוב עוד רגע."
    finally:
        stop.set()

    send_text(agent.token, chat_id, prefix + answer)
    if VOICE_REPLIES and len(answer) <= VOICE_MAX_CHARS:
        send_voice(agent.token, chat_id, answer)
    log.info("%s -> %.120s", agent.key, answer)


def poll_loop(agent):
    log.info("%s: polling started (@%s)", agent.key, agent.name)
    offset = None
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{agent.token}/getUpdates",
                params={"timeout": 50, "offset": offset},
                timeout=70,
            ).json()
            if not r.get("ok"):
                log.error("%s: getUpdates not ok: %s", agent.key, str(r)[:200])
                time.sleep(10)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        handle_message(agent, u["message"])
                    except Exception:
                        log.exception("%s: handler crashed", agent.key)
                elif "callback_query" in u:
                    try:
                        handle_callback(agent, u["callback_query"])
                    except Exception:
                        log.exception("%s: callback crashed", agent.key)
        except Exception:
            log.exception("%s: poll error", agent.key)
            time.sleep(5)


# ---------- inline button taps (e.g. "I'm calling this person") ----------

CB_TASKS_BOARD = "5094863814"     # מנהל משימות חכם
CB_STATUS_COL = "color_mm2h9y51"  # סטטוס ביצוע
CB_DONE_LABEL = "בוצע"


def _btn_label(msg, data):
    for row in (msg.get("reply_markup") or {}).get("inline_keyboard") or []:
        for b in row:
            if b.get("callback_data") == data:
                t = b.get("text", "")
                return t.split(". ", 1)[-1] if ". " in t else t
    return ""


def handle_callback(agent, cq):
    cq_id = cq["id"]
    data = cq.get("data", "") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id != ALLOWED_CHAT_ID:
        return
    if not data.startswith("cbdone:"):
        tg(agent.token, "answerCallbackQuery", callback_query_id=cq_id)
        return

    item_id = data.split(":", 1)[1]
    name = _btn_label(msg, data)
    try:
        monday_api(
            "mutation($b:ID!,$i:ID!,$v:JSON!){change_multiple_column_values("
            "board_id:$b,item_id:$i,column_values:$v){id}}",
            {"b": CB_TASKS_BOARD, "i": item_id,
             "v": json.dumps({CB_STATUS_COL: {"label": CB_DONE_LABEL}}, ensure_ascii=False)},
        )
    except Exception:
        log.exception("cbdone failed for %s", item_id)
        tg(agent.token, "answerCallbackQuery", callback_query_id=cq_id, text="שגיאה, נסה שוב")
        return

    tg(agent.token, "answerCallbackQuery", callback_query_id=cq_id, text=f"✓ מתקשר ל{name}")
    # edit the message: drop the tapped button, note it was marked
    kb = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    new_kb = [row for row in kb if not any(b.get("callback_data") == data for b in row)]
    new_text = (msg.get("text") or "") + (f"\n☑️ מתקשר ל{name}" if name else "")
    params = {"chat_id": chat_id, "message_id": msg["message_id"], "text": new_text}
    if new_kb:
        params["reply_markup"] = {"inline_keyboard": new_kb}
    tg(agent.token, "editMessageText", **params)
    log.info("%s: marked callback done item=%s (%s)", agent.key, item_id, name)


# ---------- finance mirror: 💰 tasks flow to the finance intake board ----------

FIN_SOURCE_BOARD = "5094863814"   # מנהל משימות חכם (meeting-agent writes tasks here)
FIN_TARGET_BOARD = "5094858663"   # שער הקלט והנתב — הלוח הפיננסי
FIN_TARGET_GROUP = "topics"       # 📥 קלט חדש מהשטח
FIN_DATE_COL = "date_mm2htz77"    # תאריך ביצוע (אין פריט בלי תאריך)
FIN_AMOUNT_COL = "numeric_mm2hhty8"  # סכום גולמי
FIN_SOURCE_DUE_COL = "date_mm2hx1by"
FIN_MIRROR_SEC = int(os.getenv("FIN_MIRROR_SEC", "600"))
MIRROR_STATE_PATH = os.path.join(BASE, "mirror_state.json")


def monday_api(query, variables=None):
    token = os.getenv("MONDAY_API_TOKEN", "")
    r = requests.post(
        "https://api.monday.com/v2",
        headers={"Authorization": token, "API-Version": "2024-10"},
        json={"query": query, "variables": variables or {}},
        timeout=40,
    )
    data = r.json()
    if "errors" in data:
        raise RuntimeError(str(data["errors"])[:300])
    return data["data"]


def _extract_amount(text):
    import re
    nums = [int(m.replace(",", "")) for m in re.findall(r"\d[\d,]{1,}", text)]
    nums = [n for n in nums if n >= 10]  # ignore stray single digits
    return max(nums) if nums else None


def finance_mirror_once():
    try:
        with open(MIRROR_STATE_PATH, encoding="utf-8") as f:
            done = json.load(f).get("done", {})
    except (FileNotFoundError, json.JSONDecodeError):
        done = {}

    data = monday_api(
        "query ($board: [ID!]) { boards(ids: $board) { items_page(limit: 100) { "
        "items { id name column_values(ids: [\"" + FIN_SOURCE_DUE_COL + "\"]) { id text } } } } }",
        {"board": [FIN_SOURCE_BOARD]},
    )
    items = data["boards"][0]["items_page"]["items"]
    moved = 0
    for it in items:
        if not it["name"].startswith("💰") or it["id"] in done:
            continue
        due = next((cv["text"] for cv in it["column_values"] if cv["text"]), "") \
            or time.strftime("%Y-%m-%d")
        cols = {FIN_DATE_COL: {"date": due}}
        amount = _extract_amount(it["name"])
        if amount:
            cols[FIN_AMOUNT_COL] = str(amount)
        created = monday_api(
            "mutation ($board: ID!, $group: String!, $name: String!, $cols: JSON!) { "
            "create_item(board_id: $board, group_id: $group, item_name: $name, "
            "column_values: $cols) { id } }",
            {"board": FIN_TARGET_BOARD, "group": FIN_TARGET_GROUP,
             "name": it["name"], "cols": json.dumps(cols, ensure_ascii=False)},
        )
        new_id = created["create_item"]["id"]
        monday_api(
            "mutation ($item: ID!, $body: String!) { create_update(item_id: $item, body: $body) { id } }",
            {"item": new_id,
             "body": f"הועבר אוטומטית מהסוכן המסכם. פריט המקור במנהל משימות חכם: {it['id']}"},
        )
        done[it["id"]] = new_id
        moved += 1
        log.info("finance-mirror: %s -> item %s", it["name"][:60], new_id)
        tok = os.getenv("BOT_TOKEN_TIMEKEEPER", "").strip()
        if tok:
            tg(tok, "sendMessage", chat_id=ALLOWED_CHAT_ID,
               text=f"💰 עבר אוטומטית ללוח הפיננסי (שער הקלט):\n{it['name']}\nמזהה: {new_id}")

    if moved:
        with open(MIRROR_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"done": done}, f, ensure_ascii=False, indent=1)
    return moved


def finance_mirror_loop():
    while True:
        try:
            finance_mirror_once()
        except Exception:
            log.exception("finance mirror cycle failed")
        time.sleep(FIN_MIRROR_SEC)


# ---------- watchdog: alert Nachi when a sibling service breaks ----------

HEARTBEAT_PATH = os.path.join(BASE, "heartbeat.txt")
WATCHDOG_SEC = int(os.getenv("WATCHDOG_SEC", "600"))


def _alert_token():
    for env_key in ("BOT_TOKEN_TIMEKEEPER", "BOT_TOKEN_MONDAY", "BOT_TOKEN_FINANCE", "BOT_TOKEN_SUMMARIZER"):
        tok = os.getenv(env_key, "").strip()
        if tok:
            return tok
    return ""


def _tizkoran_ok():
    try:
        return bool(requests.get("http://localhost:8787/health", timeout=5).json().get("ok"))
    except Exception:
        return False


def _meeting_agent_ok():
    """True if a python main.py process other than ours is alive (the summarizer service)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' or Name = 'py.exe'\" | "
             "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
        )
        procs = json.loads(out.stdout or "[]")
        if isinstance(procs, dict):
            procs = [procs]
        me = os.getpid()
        for p in procs:
            cl = p.get("CommandLine") or ""
            if "main.py" in cl and "uvicorn" not in cl and p.get("ProcessId") != me:
                return True
        return False
    except Exception:
        log.exception("meeting-agent check failed")
        return None  # unknown — do not alert


def watchdog_loop():
    token = _alert_token()
    last = {}
    while True:
        try:
            with open(HEARTBEAT_PATH, "w") as f:
                f.write(str(int(time.time())))
            status = {"שומר זמן (שרת התזכורות)": _tizkoran_ok()}
            ma = _meeting_agent_ok()
            if ma is not None:
                status["סוכן המסכם (שירות הסיכומים)"] = ma
            for name, ok in status.items():
                prev = last.get(name)
                if prev is True and ok is False and token:
                    tg(token, "sendMessage", chat_id=ALLOWED_CHAT_ID,
                       text=f"🚨 התראת מערכת: {name} הפסיק לעבוד.\nאפשר לכתוב לי \"מה קרה?\" ואבדוק לעומק.")
                elif prev is False and ok is True and token:
                    tg(token, "sendMessage", chat_id=ALLOWED_CHAT_ID, text=f"✅ {name} חזר לעבוד.")
                last[name] = ok
        except Exception:
            log.exception("watchdog cycle failed")
        time.sleep(WATCHDOG_SEC)


def main():
    with open(os.path.join(BASE, "agents.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    threads = []
    for key, c in cfg.items():
        a = Agent(key, c)
        if not a.token:
            log.info("%s: no bot token yet, skipped", key)
            continue
        t = threading.Thread(target=poll_loop, args=(a,), name=key, daemon=True)
        t.start()
        threads.append(t)
    if not threads:
        log.error("no agents have tokens — nothing to do")
        return
    threading.Thread(target=watchdog_loop, name="watchdog", daemon=True).start()
    threading.Thread(target=finance_mirror_loop, name="fin-mirror", daemon=True).start()
    log.info("Agent Gate is up with %d agents (+watchdog every %ss, +finance mirror every %ss)",
             len(threads), WATCHDOG_SEC, FIN_MIRROR_SEC)

    def self_test():
        try:
            env = dict(os.environ)
            for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
                env.pop(var, None)
            out = subprocess.run(
                [CLAUDE_BIN, "-p", "--dangerously-skip-permissions"],
                input="ענה במילה אחת: תקין",
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=120,
            )
            log.info("claude self-test rc=%s out=%.60s err=%.120s",
                     out.returncode, (out.stdout or "").strip(), (out.stderr or "").strip())
        except Exception:
            log.exception("claude self-test failed")

    threading.Thread(target=self_test, name="selftest", daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
