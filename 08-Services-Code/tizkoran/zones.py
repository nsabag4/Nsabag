"""Zone definitions from zones.yaml (must mirror the MacroDroid geofences)."""
import logging
import os

import yaml

log = logging.getLogger("tizkoran.zones")
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones.yaml")


def zone_for_ssid(ssid: str):
    """Map a Wi-Fi network name to a zone_id (or None).

    Matching is case-insensitive and ignores surrounding quotes,
    because Android sometimes reports the SSID as "MyNetwork" (quoted).
    """
    if not ssid:
        return None
    wanted = str(ssid).strip().strip('"').strip().lower()
    if not wanted or wanted == "<unknown ssid>":
        return None
    for zone_id, zone in load_zones().items():
        for candidate in zone.get("wifi_ssids") or []:
            if str(candidate).strip().strip('"').strip().lower() == wanted:
                return zone_id
    return None


def load_zones() -> dict:
    """{zone_id: {id, name, address, lat, lon, wifi_ssids: [...], errands: [...]}}"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("zones.yaml not found")
        return {}
    except yaml.YAMLError as exc:
        log.error("zones.yaml is invalid: %s", exc)
        return {}
    return {z["id"]: z for z in data.get("zones", []) if isinstance(z, dict) and "id" in z}
