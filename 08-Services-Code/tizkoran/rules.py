"""The brain of Tizkoran: decides when to alert and what to suggest.

1. check_leave_alerts  - runs every 2 minutes; "leave in X minutes" + "leave NOW".
2. on_car_connected    - Bluetooth car detected; suggests client callbacks
                         sized to the length of the drive (from monday.com).
3. on_zone_enter/exit  - geofence events; reminds about errands in the area.
"""
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

import calendar_sync
import monday_client
import notify
import state
import travel
import zones

log = logging.getLogger("tizkoran.rules")
TZ = ZoneInfo("Asia/Jerusalem")

_travel_cache: dict = {}  # {cache_key: (unix_ts, minutes_or_None)}
_TRAVEL_CACHE_SEC = 600   # ask Google again at most every 10 minutes per event


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now() -> dt.datetime:
    return dt.datetime.now(TZ)


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%H:%M")


def _quiet_now() -> bool:
    raw = os.getenv("QUIET_HOURS", "").strip()  # e.g. "23-07"
    if not raw or "-" not in raw:
        return False
    try:
        start_h, end_h = (int(x) for x in raw.split("-"))
    except ValueError:
        return False
    h = _now().hour
    if start_h <= end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h


def _origin():
    """Best-known current position: fresh GPS -> current zone -> home address."""
    loc = state.get_fresh_location(max_age_min=_int_env("LOCATION_MAX_AGE_MIN", 20))
    if loc:
        return {"lat": loc["lat"], "lon": loc["lon"]}
    zone_id = state.get("current_zone")
    if zone_id:
        z = zones.load_zones().get(zone_id)
        if z:
            if z.get("lat") and z.get("lon"):
                return {"lat": z["lat"], "lon": z["lon"]}
            if z.get("address"):
                return z["address"]
    return os.getenv("HOME_ADDRESS", "")


def _travel_cached(origin, ev, now: dt.datetime):
    """Travel minutes to an event, cached to keep API usage inside the free tier."""
    key = f"{ev['uid']}|{ev['start'].isoformat()}"
    hit = _travel_cache.get(key)
    if hit and now.timestamp() - hit[0] < _TRAVEL_CACHE_SEC:
        return hit[1]
    minutes = travel.travel_minutes(origin, ev["location"], departure=now)
    _travel_cache[key] = (now.timestamp(), minutes)
    return minutes


def _still_at_origin():
    """Is the phone still at a known origin (home/office Wi-Fi connected)?
    True = still here (hasn't left), False = confirmed left both, None = unknown.
    Used so the departure alerts escalate only while he genuinely hasn't left —
    the point is to fight Nachi's chronic lateness with real location, not a clock."""
    home = state.get("at_home")
    office = state.get("at_office")
    if home is True or office is True:
        return True
    if home is False and office is False:
        return False
    return None  # partial/no signal — don't suppress alerts


# ---------- 1) leave-on-time alerts ----------

def check_leave_alerts():
    try:
        _check_leave_alerts()
    except Exception:  # never let the scheduler die
        log.exception("check_leave_alerts failed")


def _check_leave_alerts():
    if _quiet_now():
        return

    park = _int_env("PARKING_BUFFER_MIN", 10)
    walk = _int_env("WALKING_BUFFER_MIN", 5)
    lead = _int_env("ALERT_LEAD_MIN", 15)
    default_travel = _int_env("DEFAULT_TRAVEL_MIN", 30)

    now = _now()
    origin = _origin()

    for ev in calendar_sync.get_upcoming_events(hours=8):
        if ev["start"] <= now:
            continue

        role = ev.get("role", "travel")
        mins_to_start = (ev["start"] - now).total_seconds() / 60
        date_key = ev["start"].strftime("%Y%m%d%H%M")

        if role == "office":
            # Meeting AT the office (client comes to Nachi) — no travel time.
            # Reason (frozen): but if he's confirmed away from the office (office
            # Wi-Fi "10" not connected), he must head back — so alert. When his
            # location is unknown (no Wi-Fi signal yet) we stay quiet, to avoid
            # false "you're away" alarms before the office Wi-Fi macro is live.
            at_office = state.get("at_office")  # True / False / None(unknown)
            office_lead = _int_env("OFFICE_ALERT_LEAD_MIN", 30)
            if at_office is False and 0 < mins_to_start <= office_lead:
                if state.mark_alerted(f"office_away:{ev['uid']}:{date_key}"):
                    notify.send(
                        "🏢 פגישה במשרד\n"
                        f"{ev['title']} בשעה {_fmt(ev['start'])} (בעוד {max(1, round(mins_to_start))} דק').\n"
                        "אתה לא מחובר לרשת המשרד — כדאי לחזור למשרד."
                    )
            continue

        if not ev["location"]:
            # online/phone meeting — no travel to compute, just a heads-up
            if mins_to_start <= lead:
                if state.mark_alerted(f"meet:{ev['uid']}:{date_key}"):
                    notify.send(
                        "⏰ תזכורת פגישה\n"
                        f"{ev['title']} מתחיל בעוד {max(1, round(mins_to_start))} דקות ({_fmt(ev['start'])})."
                    )
            continue
        # Only query traffic when the event is close enough to matter.
        query_window = max(default_travel * 2 + park + walk + lead, 150)
        if mins_to_start > query_window:
            continue

        minutes = _travel_cached(origin, ev, now)
        with_traffic = minutes is not None
        if minutes is None:
            minutes = default_travel
        traffic_note = " (כולל עומסים)" if with_traffic else " (הערכה, ללא נתוני עומסים)"

        leave_at = ev["start"] - dt.timedelta(minutes=minutes + park + walk)
        date_key = ev["start"].strftime("%Y%m%d%H%M")

        expected_arrival = now + dt.timedelta(minutes=minutes + park + walk)
        late_by = (expected_arrival - ev["start"]).total_seconds() / 60
        mins_to_leave = (leave_at - now).total_seconds() / 60
        prepare_lead = _int_env("PREPARE_LEAD_MIN", 20)
        origin_state = _still_at_origin()       # True / False / None
        left_already = origin_state is False    # confirmed he set out
        home_note = "אתה עדיין בבית — " if origin_state is True else ""

        # LATE — even leaving now he'd arrive after the start (traffic ate the
        # buffer). Pop the destination's phone so he can call ahead. Fires
        # whether he's home or already on the road (useful either way).
        if late_by > _int_env("LATE_THRESHOLD_MIN", 2):
            if now < ev["start"] + dt.timedelta(minutes=45):
                if state.mark_alerted(f"late:{ev['uid']}:{date_key}"):
                    phone = monday_client.find_phone_for_meeting(ev["title"])
                    phone_line = (f"\n📞 להתקשר ולעדכן: {phone}" if phone
                                  else "\n📞 כדאי להתקשר ליעד ולעדכן שאתה בדרך.")
                    notify.send(
                        f"⚠️ {home_note}צפוי איחור\n"
                        f"פגישה: {ev['title']} בשעה {_fmt(ev['start'])}\n"
                        f"מקום: {ev['location']}\n"
                        f"נסיעה ~{minutes} דק'{traffic_note} — צפי איחור ~{round(late_by)} דק'."
                        + phone_line
                    )
            continue

        # He set out on time (Wi-Fi confirms he left home/office) — no nags.
        if left_already:
            continue

        # Escalating departure alerts (only while he hasn't left / location unknown):
        if lead < mins_to_leave <= prepare_lead:
            # PREPARE — earliest heads-up: get ready.
            if state.mark_alerted(f"prep:{ev['uid']}:{date_key}"):
                notify.send(
                    f"🔔 {home_note}תתכונן לצאת\n"
                    f"פגישה: {ev['title']} בשעה {_fmt(ev['start'])} — {ev['location']}\n"
                    f"יציאה בעוד ~{round(mins_to_leave)} דק' (עד {_fmt(leave_at)})."
                )
        elif 0 < mins_to_leave <= lead:
            # LEAVE-IN — the lead window.
            if state.mark_alerted(f"lead:{ev['uid']}:{date_key}"):
                left = max(1, round(mins_to_leave))
                notify.send(
                    "⏰ תזכורת יציאה\n"
                    f"פגישה: {ev['title']} בשעה {_fmt(ev['start'])}\n"
                    f"מקום: {ev['location']}\n"
                    f"נסיעה: ~{minutes} דק'{traffic_note} + {park} דק' חניה + {walk} דק' הליכה\n"
                    f"לצאת בעוד {left} דקות (עד {_fmt(leave_at)})."
                )
        elif -10 <= mins_to_leave <= 0:
            # LEAVE-NOW — at/just past leave time, still not 'late'.
            if state.mark_alerted(f"go:{ev['uid']}:{date_key}"):
                notify.send(
                    f"🚨 {home_note}לצאת עכשיו!\n"
                    f"{ev['title']} בשעה {_fmt(ev['start'])} – {ev['location']}\n"
                    f"נסיעה ~{minutes} דק' + חניה + הליכה. כל דקה מעכשיו = איחור."
                )


# ---------- 2) car mode: turn drive time into callbacks ----------

def on_car_connected():
    try:
        _on_car_connected()
    except Exception:
        log.exception("on_car_connected failed")


_CAR_ALERT_GAP_SEC = 15 * 60


def _on_car_connected():
    state.set("in_car", True)
    now = _now()

    # --- trip capture (start) ---
    # Record the trip start so we can measure the real duration on disconnect.
    # Reason (frozen): a drive not in the calendar is "not recorded => doesn't
    # exist"; and Nachi can hold 2-3 things in his head but 5-6 slip away — so
    # the system must remember trips for him, and learn real drive times.
    ev_now = calendar_sync.next_event_with_location(within_hours=5)
    if not state.get("trip_start_ts"):
        state.set("trip_start_ts", now.timestamp())
        state.set("trip_start_hhmm", _fmt(now))
        state.set("trip_start_meeting", ev_now["title"] if ev_now else "")

    # A Bluetooth reconnect mid-drive (tunnel, engine restart) must not spam:
    # rolling 15-minute window. The timestamp is claimed up front so a reconnect
    # during the (slow) smart-pick work can't double-send, and rolled back on
    # failure so the next reconnect gets another chance to deliver.
    now_ts = now.timestamp()
    try:
        last_ts = float(state.get("last_car_alert_ts") or 0)
    except (TypeError, ValueError):
        last_ts = 0
    if now_ts - last_ts < _CAR_ALERT_GAP_SEC:
        return
    state.set("last_car_alert_ts", now_ts)

    sent = False
    try:
        sent = _send_car_message(now)
    finally:
        if not sent:
            state.set("last_car_alert_ts", last_ts)


# Urgency labels on the tasks board (color_mm2hhbmg): קריטי / דחוף / רגיל / כשיהיה זמן
_URGENCY_ORDER = ["קריטי", "דחוף", "רגיל", "כשיהיה זמן"]


def _urgency_rank(c) -> int:
    u = c.get("urgency") or ""
    for i, label in enumerate(_URGENCY_ORDER):
        if label in u:
            return i
    return 2  # unknown => treat as רגיל


def _send_car_message(now) -> bool:
    """Entering the car => there's a drive. Always send the ranked call list
    with a tap-to-mark button per person. Ranking is by the urgency column
    (instant, no AI wait). If a located meeting is coming up, show its live
    travel time as context — but the list is sent regardless."""
    default_travel = _int_env("DEFAULT_TRAVEL_MIN", 45)
    max_calls = _int_env("CAR_LIST_MAX", 6)

    header = "🚗 נכנסת לרכב."
    ev = calendar_sync.next_event_with_location(within_hours=5)
    if ev:
        live = travel.travel_minutes(_origin(), ev["location"], departure=now)
        minutes = live if live is not None else default_travel
        source = "עומסים חיים" if live is not None else "הערכה"
        header = (
            f"🚗 בדרך אל: {ev['title']} בשעה {_fmt(ev['start'])}\n"
            f"📍 {ev['location']} · ~{minutes} דק' ({source})"
        )

    pool = monday_client.get_pending_callbacks(limit=_int_env("SMART_PRIORITY_POOL", 20))
    if pool is None:  # monday unreachable — say so, don't claim the board is empty
        return notify.send(header + "\n(לא הצלחתי לבדוק במאנדיי מי ממתין לחזרה)")
    if not pool:
        return notify.send(header + "\nאין כרגע שיחות ממתינות. נסיעה טובה 🚗")

    ranked = sorted(pool, key=lambda c: (_urgency_rank(c), c.get("due") or "9999-99-99"))
    ranked = ranked[:max_calls]

    lines = [header, "", "📞 שיחות לפי דחיפות — הקש למי שאתה מתקשר, השאר יחכו לפעם הבאה:"]
    buttons = []
    for i, c in enumerate(ranked, start=1):
        phone = f" · {c['phone']}" if c.get("phone") else ""
        urgency = f" [{c['urgency']}]" if c.get("urgency") else ""
        lines.append(f"{i}. {c['name']}{phone}{urgency}")
        label = f"📞 {i}. {c['name']}"[:60]
        buttons.append([{"text": label, "callback_data": f"cbdone:{c['id']}"}])
    return notify.send("\n".join(lines), buttons=buttons)


def on_car_disconnected():
    try:
        _on_car_disconnected()
    except Exception:
        log.exception("on_car_disconnected failed")


def _on_car_disconnected():
    state.set("in_car", False)
    start_ts = state.get("trip_start_ts")
    if not start_ts:
        return
    now = _now()
    minutes = round((now.timestamp() - float(start_ts)) / 60.0)
    meeting = state.get("trip_start_meeting") or ""
    start_hhmm = state.get("trip_start_hhmm") or ""
    # clear the trip markers so the next connect starts fresh
    for k in ("trip_start_ts", "trip_start_hhmm", "trip_start_meeting"):
        state.set(k, None)

    if minutes < _int_env("TRIP_MIN_MINUTES", 4):
        return  # false start / very short hop — ignore

    # Record every trip (real measured duration => learning + a log that exists).
    trips = state.get("trips") or []
    trips.append({
        "date": now.strftime("%Y-%m-%d"),
        "start": start_hhmm,
        "end": _fmt(now),
        "minutes": minutes,
        "meeting": meeting,
    })
    state.set("trips", trips[-100:])

    # A trip with no matching calendar event is the dangerous one — surface it
    # now, before it slips away. (Tracked trips are recorded silently to avoid
    # flooding; their time was already shown before departure.)
    if not meeting and not _quiet_now():
        notify.send(
            "📓 נסיעה לא מתועדת ביומן\n"
            f"יצאת {start_hhmm}, חזרת {_fmt(now)} (~{minutes} דק').\n"
            "רוצה לתעד? כתוב לשומר זמן: נסיעה — <לאן ולמה>"
        )


# ---------- 3) zone-based reminders ----------

def on_zone_enter(zone_id: str):
    try:
        _on_zone_enter(zone_id)
    except Exception:
        log.exception("on_zone_enter failed")


def _on_zone_enter(zone_id: str):
    state.set("current_zone", zone_id)
    # Track home/office presence for the "you're away / did you leave" alerts.
    state.set("at_office", zone_id == "office")
    state.set("at_home", zone_id == "home")
    zone = zones.load_zones().get(zone_id)
    if not zone:
        log.warning("unknown zone_id: %s", zone_id)
        return
    if _quiet_now():
        return

    day_key = _now().strftime("%Y%m%d")
    if not state.mark_alerted(f"zone:{zone_id}:{day_key}"):
        return  # already reminded today for this zone

    items = [{"name": e} for e in zone.get("errands", []) if e]
    items += monday_client.get_tasks_for_zone(zone.get("name", ""), limit=5)
    if not items:
        return

    lines = [f"📍 אתה באזור: {zone.get('name', zone_id)}. שווה לנצל:"]
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. {it['name']}")
    notify.send("\n".join(lines))


def on_zone_exit(zone_id: str):
    if state.get("current_zone") == zone_id:
        state.set("current_zone", None)
    if zone_id == "office":  # left the office Wi-Fi => confirmed away
        state.set("at_office", False)
    if zone_id == "home":  # left home Wi-Fi => confirmed he set out
        state.set("at_home", False)
