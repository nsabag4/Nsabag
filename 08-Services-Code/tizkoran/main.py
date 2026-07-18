"""Tizkoran server: receives sensor events from the phone (MacroDroid)
and runs the periodic leave-on-time checker."""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402

import guard  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
import state  # noqa: E402
import zones  # noqa: E402
import monday_webhook  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tizkoran")

app = FastAPI(title="Tizkoran")
app.include_router(monday_webhook.router)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


def _check_secret(data: dict) -> None:
    if not WEBHOOK_SECRET or data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")


def _maybe_update_location(data: dict) -> bool:
    lat, lon = data.get("lat"), data.get("lon")
    try:
        if lat is not None and lon is not None:
            state.set_location(float(lat), float(lon))
            return True
    except (TypeError, ValueError):
        pass
    return False


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/event/test")
async def event_test(request: Request):
    data = await request.json()
    _check_secret(data)
    notify.send("✅ תזכורן מחובר ועובד. הטלפון והשרת מדברים.")
    return {"ok": True}


@app.post("/event/car")
async def event_car(request: Request, background_tasks: BackgroundTasks):
    """Body: {"secret": "...", "action": "connected" | "disconnected"}
    The connected flow may wait on traffic + smart prioritization, so it runs
    in the background — the phone gets an instant 200 and never times out."""
    data = await request.json()
    _check_secret(data)
    _maybe_update_location(data)
    if data.get("action", "connected") == "connected":
        background_tasks.add_task(rules.on_car_connected)
    else:
        rules.on_car_disconnected()
    return {"ok": True}


@app.post("/event/zone")
async def event_zone(request: Request):
    """Body: {"secret": "...", "zone_id": "...", "action": "enter" | "exit"}"""
    data = await request.json()
    _check_secret(data)
    _maybe_update_location(data)
    zone_id = str(data.get("zone_id", "")).strip()
    if not zone_id:
        raise HTTPException(status_code=400, detail="missing zone_id")
    if data.get("action", "enter") == "enter":
        rules.on_zone_enter(zone_id)
    else:
        rules.on_zone_exit(zone_id)
    return {"ok": True}


@app.post("/event/wifi")
async def event_wifi(request: Request):
    """Wi-Fi is a location sensor: connecting to a known network = arrived.
    Body: {"secret": "...", "ssid": "...", "action": "connected" | "disconnected"}
    One MacroDroid macro sends the SSID of *any* network; the mapping from
    network name to zone lives in zones.yaml (wifi_ssids), so adding a new
    place never requires touching the phone again."""
    data = await request.json()
    _check_secret(data)
    _maybe_update_location(data)
    ssid = str(data.get("ssid", "")).strip()
    zone_id = zones.zone_for_ssid(ssid)
    action = data.get("action", "connected")
    if action == "connected":
        if zone_id:
            rules.on_zone_enter(zone_id)
        else:
            log.info("Wi-Fi '%s' is not mapped to any zone (ignored)", ssid)
    else:  # disconnected
        if zone_id:
            rules.on_zone_exit(zone_id)
    return {"ok": True, "zone": zone_id}


@app.post("/event/location")
async def event_location(request: Request):
    """Optional: the phone can push a fresh GPS fix.
    Body: {"secret": "...", "lat": 32.08, "lon": 34.88}"""
    data = await request.json()
    _check_secret(data)
    if not _maybe_update_location(data):
        raise HTTPException(status_code=400, detail="missing lat/lon")
    return {"ok": True}


scheduler = BackgroundScheduler(timezone="Asia/Jerusalem")
scheduler.add_job(rules.check_leave_alerts, "interval", minutes=2, id="leave_alerts")
scheduler.add_job(guard.check_agent_gate, "interval", minutes=10, id="guard_gate")
scheduler.add_job(state.prune, "cron", hour=4, minute=0, id="prune")
scheduler.start()

log.info("Tizkoran server is up")
