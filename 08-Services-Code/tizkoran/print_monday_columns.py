"""Helper: prints all columns (id + title) of a monday.com board,
so you can copy the right ids into the .env file.

Usage:
    python print_monday_columns.py            (uses MONDAY_CALLBACKS_BOARD_ID from .env)
    python print_monday_columns.py 123456789  (any board id)
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

board_id = sys.argv[1] if len(sys.argv) > 1 else os.getenv("MONDAY_CALLBACKS_BOARD_ID", "")
token = os.getenv("MONDAY_API_TOKEN", "")

if not board_id or not token:
    print("חסר טוקן או מזהה לוח. מלא MONDAY_API_TOKEN ו-MONDAY_CALLBACKS_BOARD_ID בקובץ .env")
    sys.exit(1)

q = "query($b:[ID!]){ boards(ids:$b){ name columns{ id title type } } }"
r = requests.post(
    "https://api.monday.com/v2",
    json={"query": q, "variables": {"b": [board_id]}},
    headers={"Authorization": token, "API-Version": "2024-10"},
    timeout=25,
)
data = r.json()

if "errors" in data:
    print("שגיאה ממאנדיי:", data["errors"])
    sys.exit(1)

boards = data.get("data", {}).get("boards") or []
if not boards:
    print("לא נמצא לוח עם המזהה הזה.")
    sys.exit(1)

board = boards[0]
print(f"לוח: {board['name']}")
print("עמודות (העתק את ה-id המתאים אל .env):")
for c in board["columns"]:
    print(f"  id={c['id']:<24} type={c['type']:<12} כותרת: {c['title']}")
