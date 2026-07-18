"""Pull pending-callback clients and zone-tagged tasks from monday.com."""
import logging
import os

import requests

log = logging.getLogger("tizkoran.monday")
_API = "https://api.monday.com/v2"


def _query(query: str, variables: dict):
    token = os.getenv("MONDAY_API_TOKEN", "")
    if not token:
        return None
    try:
        r = requests.post(
            _API,
            json={"query": query, "variables": variables},
            headers={"Authorization": token, "API-Version": "2024-10"},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            log.error("monday errors: %s", data["errors"])
            return None
        return data.get("data")
    except requests.RequestException as exc:
        log.error("monday request failed: %s", exc)
        return None


def _board_items(board_id: str):
    """Items of a board, or None when the API call failed (vs. [] for empty)."""
    q = """
    query ($board: [ID!]) {
      boards(ids: $board) {
        items_page(limit: 100) {
          items { id name column_values { id text } }
        }
      }
    }
    """
    data = _query(q, {"board": [board_id]})
    if data is None:
        return None
    boards = data.get("boards") or []
    if not boards:
        return None  # board not found / no access — an error, not an empty board
    return boards[0].get("items_page", {}).get("items", [])


def _col(item, col_id: str) -> str:
    for cv in item.get("column_values", []):
        if cv.get("id") == col_id:
            return (cv.get("text") or "").strip()
    return ""


def get_pending_callbacks(limit: int = 3):
    """Items whose status is one of PENDING_STATUS_TEXTS
    -> [{id, name, phone, status, urgency, due}], or None when monday is unreachable."""
    board_id = os.getenv("MONDAY_CALLBACKS_BOARD_ID", "")
    if not board_id:
        return []
    status_col = os.getenv("MONDAY_STATUS_COLUMN_ID", "status")
    phone_col = os.getenv("MONDAY_PHONE_COLUMN_ID", "phone")
    urgency_col = os.getenv("MONDAY_URGENCY_COLUMN_ID", "")
    due_col = os.getenv("MONDAY_DUE_COLUMN_ID", "")
    pending = [
        s.strip()
        for s in os.getenv("PENDING_STATUS_TEXTS", "לחזור ללקוח,ממתין לחזרה").split(",")
        if s.strip()
    ]

    items = _board_items(board_id)
    if items is None:
        return None

    result = []
    for item in items:
        status = _col(item, status_col)
        if status in pending:
            result.append({
                "id": item.get("id"),
                "name": item.get("name", ""),
                "phone": _col(item, phone_col),
                "status": status,
                "urgency": _col(item, urgency_col) if urgency_col else "",
                "due": _col(item, due_col) if due_col else "",
            })
        if len(result) >= limit:
            break
    return result


def find_phone_for_meeting(title: str):
    """Phone of the contact named in a meeting title, or None.

    Used when Nachi is running late: surface the destination's number so he can
    call ahead. Matches a contact whose full name appears inside the title.
    """
    board_id = os.getenv("MONDAY_CONTACTS_BOARD_ID", "")
    phone_col = os.getenv("MONDAY_CONTACTS_PHONE_COL", "contact_phone")
    if not board_id or not title:
        return None
    for item in _board_items(board_id) or []:
        name = (item.get("name") or "").strip()
        toks = [t for t in name.split() if len(t) >= 2]
        if toks and all(t in title for t in toks):
            phone = _col(item, phone_col)
            if phone:
                return phone
    return None


def get_tasks_for_zone(zone_name: str, limit: int = 5):
    """Items whose zone column text equals the zone name -> [{name, id}]."""
    board_id = os.getenv("MONDAY_ERRANDS_BOARD_ID", "") or os.getenv("MONDAY_CALLBACKS_BOARD_ID", "")
    zone_col = os.getenv("MONDAY_ZONE_COLUMN_ID", "")
    if not board_id or not zone_col or not zone_name:
        return []

    result = []
    for item in _board_items(board_id) or []:
        if _col(item, zone_col) == zone_name:
            result.append({"id": item.get("id"), "name": item.get("name", "")})
        if len(result) >= limit:
            break
    return result
