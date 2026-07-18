"""Tiny JSON state store: alert dedupe, car/zone status, last GPS fix."""
import json
import os
import threading
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


def get(key, default=None):
    with _LOCK:
        return _load().get(key, default)


def set(key, value):  # noqa: A001 - deliberate simple API
    with _LOCK:
        data = _load()
        data[key] = value
        _save(data)


def mark_alerted(key: str) -> bool:
    """True if this alert was NOT sent yet (caller should send it now)."""
    with _LOCK:
        data = _load()
        sent = data.setdefault("alerted", {})
        if key in sent:
            return False
        sent[key] = int(time.time())
        _save(data)
        return True


def set_location(lat: float, lon: float) -> None:
    set("last_location", {"lat": lat, "lon": lon, "ts": int(time.time())})


def get_fresh_location(max_age_min: int = 20):
    loc = get("last_location")
    if loc and time.time() - loc.get("ts", 0) <= max_age_min * 60:
        return loc
    return None


def prune(days: int = 3) -> None:
    """Drop old dedupe keys so the file stays small."""
    cutoff = time.time() - days * 86400
    with _LOCK:
        data = _load()
        sent = data.get("alerted", {})
        data["alerted"] = {k: v for k, v in sent.items() if v >= cutoff}
        _save(data)
