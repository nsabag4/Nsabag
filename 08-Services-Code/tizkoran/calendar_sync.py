"""Read upcoming events from one or more Google Calendar secret ICS URLs."""
import datetime as dt
import logging
import os
import time
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

log = logging.getLogger("tizkoran.calendar")
TZ = ZoneInfo("Asia/Jerusalem")

_cache: dict = {}  # url -> {"ts": float, "raw": bytes}
_CACHE_SEC = 240  # refetch each ICS at most every 4 minutes


def _split(raw):
    return [u.strip() for u in (raw or "").replace("\n", ",").split(",") if u.strip()]


def _sources():
    """[(url, role)] — role 'office' (meetings AT the office, no travel) or
    'travel' (out-of-office meetings needing GPS/traffic leave alerts)."""
    out = []
    seen = set()

    def add(urls, role):
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append((u, role))

    add(_split(os.getenv("CALENDAR_OFFICE_ICS", "")), "office")
    add(_split(os.getenv("CALENDAR_TRAVEL_ICS", "")), "travel")
    # legacy single/list var — treat as travel (has locations) if not classified
    add(_split(os.getenv("CALENDAR_ICS_URLS", "") or os.getenv("CALENDAR_ICS_URL", "")), "travel")
    return out


def _fetch_all():
    """[(raw, role)] for every configured calendar; a failing one is skipped."""
    out = []
    now = time.time()
    for url, role in _sources():
        hit = _cache.get(url)
        if hit and now - hit["ts"] < _CACHE_SEC:
            out.append((hit["raw"], role))
            continue
        try:
            r = requests.get(url, timeout=25)
            r.raise_for_status()
            _cache[url] = {"ts": now, "raw": r.content}
            out.append((r.content, role))
        except requests.RequestException as exc:
            log.error("ICS fetch failed (%s...): %s", url[:60], exc)
            if hit:  # stale copy is better than a blind spot
                out.append((hit["raw"], role))
    return out


def get_upcoming_events(hours: int = 10):
    """Timed events (all-day events are skipped) in the next `hours`,
    including events that already started. Merged from all calendars,
    deduped by UID, sorted by start time."""
    now = dt.datetime.now(TZ)
    window_end = now + dt.timedelta(hours=hours)

    events = []
    for raw, role in _fetch_all():
        try:
            cal = Calendar.from_ical(raw)
            for ev in recurring_ical_events.of(cal).between(now - dt.timedelta(hours=6), window_end):
                events.append((ev, role))
        except Exception as exc:  # one bad calendar must not blind the rest
            log.error("ICS parse failed: %s", exc)

    seen_uids = set()
    out = []
    for ev, role in events:
        uid = str(ev.get("UID", ""))
        if uid and uid in seen_uids:
            continue
        seen_uids.add(uid)
        dtstart = ev.get("DTSTART")
        if dtstart is None or not isinstance(dtstart.dt, dt.datetime):
            continue  # skip all-day events
        start = dtstart.dt
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        start = start.astimezone(TZ)

        dtend = ev.get("DTEND")
        if dtend is not None and isinstance(dtend.dt, dt.datetime):
            end = dtend.dt
            if end.tzinfo is None:
                end = end.replace(tzinfo=TZ)
            end = end.astimezone(TZ)
        else:
            end = start + dt.timedelta(hours=1)

        out.append(
            {
                "uid": str(ev.get("UID", "")),
                "title": str(ev.get("SUMMARY", "")).strip() or "פגישה",
                "location": str(ev.get("LOCATION", "") or "").strip(),
                "start": start,
                "end": end,
                "role": role,
            }
        )

    out.sort(key=lambda e: e["start"])
    return out


def next_event_with_location(within_hours: int = 5):
    """The next FUTURE event that has an address in its location field."""
    now = dt.datetime.now(TZ)
    for ev in get_upcoming_events(hours=within_hours):
        if ev["location"] and ev["start"] > now:
            return ev
    return None


def current_event():
    """The event happening right now, if any."""
    now = dt.datetime.now(TZ)
    for ev in get_upcoming_events(hours=1):
        if ev["start"] <= now <= ev["end"]:
            return ev
    return None
