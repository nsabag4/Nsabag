"""Run a GraphQL query against monday.com and print the JSON result.

Usage:  python C:\\dev\\agent-gate\\monday_query.py "<graphql query>" ["<variables json>"]
The token is read from C:\\dev\\agent-gate\\.env (MONDAY_API_TOKEN).
"""
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

if len(sys.argv) < 2:
    print("usage: python monday_query.py \"<graphql>\"")
    sys.exit(1)

token = os.getenv("MONDAY_API_TOKEN", "")
if not token:
    print("MONDAY_API_TOKEN missing in agent-gate .env")
    sys.exit(1)

variables = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
r = requests.post(
    "https://api.monday.com/v2",
    headers={"Authorization": token, "Content-Type": "application/json", "API-Version": "2024-10"},
    json={"query": sys.argv[1], "variables": variables},
    timeout=40,
)
out = json.dumps(r.json(), ensure_ascii=False, indent=1)
sys.stdout.reconfigure(encoding="utf-8")
print(out[:20000])
