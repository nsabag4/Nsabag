"""Driving time with live traffic.

Provider chain: TomTom (official, free tier, no card) -> Waze (unofficial,
dead as of 07.2026 — kept off by default in case it revives) -> Google
Routes API (only if a key is configured) -> None, so the caller falls back
to DEFAULT_TRAVEL_MIN.
"""
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("tizkoran.travel")
TZ = ZoneInfo("Asia/Jerusalem")

_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_WAZE_GEOCODE_URL = "https://www.waze.com/il-SearchServer/mozi"
_WAZE_ROUTE_URL = "https://www.waze.com/il-RoutingManager/routingRequest"
_WAZE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "referer": "https://www.waze.com/",
}
_geo_cache: dict = {}  # address -> (lat, lon); meeting addresses repeat a lot


def travel_minutes(origin, destination, departure: dt.datetime | None = None):
    """Minutes of driving with live traffic, or None when no provider answered."""
    if not origin or not destination:
        return None

    minutes = _tomtom_minutes(origin, destination)
    if minutes is not None:
        return minutes

    minutes = _waze_minutes(origin, destination)
    if minutes is not None:
        return minutes

    return _google_minutes(origin, destination, departure)


# ---------- TomTom (official, 2500 free calls/day, no credit card) ----------

def _tomtom_coords(value, key):
    if isinstance(value, dict) and "lat" in value and "lon" in value:
        return value["lat"], value["lon"]
    addr = str(value).strip()
    if not addr:
        return None
    cache_key = f"tt:{addr}"
    if cache_key in _geo_cache:
        return _geo_cache[cache_key]
    r = requests.get(
        f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(addr)}.json",
        params={"key": key, "countrySet": "IL", "limit": 1, "language": "he-IL"},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        log.warning("tomtom geocode found nothing for: %s", addr)
        return None
    pos = results[0]["position"]
    coords = (pos["lat"], pos["lon"])
    _geo_cache[cache_key] = coords
    return coords


def _tomtom_minutes(origin, destination):
    key = os.getenv("TOMTOM_API_KEY", "").strip()
    if not key:
        return None
    try:
        o = _tomtom_coords(origin, key)
        d = _tomtom_coords(destination, key)
        if not o or not d:
            return None
        # Matrix Routing v2 (the product enabled on Nachi's key), synchronous 1x1.
        r = requests.post(
            f"https://api.tomtom.com/routing/matrix/2?key={key}",
            json={
                "origins": [{"point": {"latitude": o[0], "longitude": o[1]}}],
                "destinations": [{"point": {"latitude": d[0], "longitude": d[1]}}],
                "options": {"routeType": "fastest", "traffic": "live",
                            "travelMode": "car", "departAt": "now"},
            },
            timeout=25,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return None
        seconds = data[0]["routeSummary"]["travelTimeInSeconds"]
        minutes = max(1, round(seconds / 60))
        log.info("tomtom(matrix): %s -> %s = %s min", origin, destination, minutes)
        return minutes
    except Exception as exc:
        log.warning("tomtom failed (%s), trying next provider", exc)
        _alert_provider_down("טום-טום")
        return None


def _alert_provider_down(provider_name: str):
    """One Telegram alert per provider per day — a configured provider is failing."""
    try:
        import datetime as _dt

        import notify
        import state
        day = _dt.date.today().strftime("%Y%m%d")
        if state.mark_alerted(f"travel_fail:{provider_name}:{day}"):
            notify.send(
                f"⚠️ בדיקת עומסי התנועה דרך {provider_name} נכשלת היום. "
                "עברתי לגיבוי — התזכורות ממשיכות לעבוד. אפשר לשאול אותי \"מה קרה?\""
            )
    except Exception:
        log.exception("provider-down alert failed")


# ---------- Waze (unofficial, no key, excellent data in Israel) ----------

def _waze_coords(value):
    """Address string or {"lat","lon"} -> (lat, lon) via Waze's own geocoder."""
    if isinstance(value, dict) and "lat" in value and "lon" in value:
        return value["lat"], value["lon"]
    addr = str(value).strip()
    if not addr:
        return None
    if addr in _geo_cache:
        return _geo_cache[addr]
    r = requests.get(
        _WAZE_GEOCODE_URL,
        params={"q": addr, "lang": "heb"},
        headers=_WAZE_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    first = data[0] if isinstance(data, list) and data else None
    loc = (first or {}).get("location")
    if not loc:
        log.warning("waze geocode found nothing for: %s", addr)
        return None
    coords = (loc["lat"], loc["lon"])
    _geo_cache[addr] = coords
    return coords


def _waze_minutes(origin, destination):
    if os.getenv("WAZE_ENABLED", "1") != "1":
        return None
    try:
        o = _waze_coords(origin)
        d = _waze_coords(destination)
        if not o or not d:
            return None
        r = requests.get(
            _WAZE_ROUTE_URL,
            params={
                "from": f"x:{o[1]} y:{o[0]}",
                "to": f"x:{d[1]} y:{d[0]}",
                "at": 0,
                "returnJSON": "true",
                "returnGeometries": "false",
                "returnInstructions": "false",
                "timeout": 60000,
                "nPaths": 1,
                "options": "AVOID_TRAILS:t",
            },
            headers=_WAZE_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        results = (data.get("response") or {}).get("results") or []
        if not results and data.get("alternatives"):
            results = data["alternatives"][0].get("response", {}).get("results", [])
        if not results:
            return None
        total_sec = sum(
            seg.get("crossTimeWithRealTime") or seg.get("crossTime") or 0
            for seg in results
        )
        if not total_sec:
            return None
        minutes = max(1, round(total_sec / 60))
        log.info("waze: %s -> %s = %s min", origin, destination, minutes)
        return minutes
    except Exception as exc:  # unofficial API — never let it break the flow
        log.warning("waze failed (%s), trying next provider", exc)
        return None


# ---------- Google Routes API (official, needs a key) ----------

def _place(value):
    if isinstance(value, dict) and "lat" in value and "lon" in value:
        return {"location": {"latLng": {"latitude": value["lat"], "longitude": value["lon"]}}}
    return {"address": str(value)}


def _google_minutes(origin, destination, departure: dt.datetime | None = None):
    key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if not key:
        return None

    dep = departure or dt.datetime.now(TZ)
    dep = max(dep, dt.datetime.now(TZ) + dt.timedelta(minutes=1))  # must be in the future

    body = {
        "origin": _place(origin),
        "destination": _place(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "departureTime": dep.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "languageCode": "he",
    }
    try:
        r = requests.post(
            _ROUTES_URL,
            json=body,
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.duration"},
            timeout=20,
        )
        r.raise_for_status()
        routes = r.json().get("routes", [])
        if not routes:
            return None
        seconds = int(str(routes[0]["duration"]).rstrip("s"))
        return max(1, round(seconds / 60))
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        log.error("Routes API failed: %s", exc)
        _alert_provider_down("גוגל")
        return None
