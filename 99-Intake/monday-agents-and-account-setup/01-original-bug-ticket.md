# כרטיס התקלה המקורי — ההודעה הראשונה בסשן (מילה במילה)

BUG TICKET — monday.com MCP tools not exposed in Claude.ai chat surface
הוראה לנחי: הדבק את הקובץ הזה, כמו שהוא, כהודעה הראשונה ב-Claude Code.
Role
You are a senior integration engineer. Diagnose and work around the failure described below. Be precise, evidence-driven, and non-destructive. Report only what actually happened.
Context / Environment

* Product surface: Claude.ai chat (web + Android app on Galaxy S24 Ultra), inside project "סבג נדל"ן".
* monday.com account: `sabag-nadlan.monday.com`.
* monday MCP server: `https://mcp.monday.com/mcp` (Streamable HTTP, OAuth).
* Claude.ai connector-registry state as retrieved on 05.07.2026:
   * `installState: "connected"`, `connected: true`
   * `installedServerId: ee0dc4b6-4c27-4eb6-98f3-dc5d5faf1e01`
   * `directoryUuid: 49e0f9ba-7d45-4fb6-b098-55eec956fbc6`
* Registry-advertised tools: `delete_item`, `get_board_items_by_name`, `create_item`, `create_update`, `get_board_schema`, `get_users_by_name`, `change_item_column_values`, `move_item_to_group` (+12 more). Companion skill docs also reference: `get_user_context`, `get_board_info`, `get_board_items_page`, `board_insights`, `get_board_activity`, `get_updates`.
Symptom
Inside Claude.ai chat conversations, the monday.com tools are never available to the model:

1. Deferred-tool search over many query variants ("monday boards", exact tool names, etc.) returns only Google Workspace / Microsoft 365 tools — monday tools are absent from the tool index.
2. Direct invocation attempts all fail with "Tool not found": `mcp__{installedServerId}__get_user_context`, `mcp__{directoryUuid}__get_user_context`, `monday.com:get_user_context`, bare `get_user_context`.
3. After the user explicitly selected "Use monday.com for this" in the connector picker, behavior was unchanged.
What DOES work (root-cause isolation)
The Anthropic Messages API, called from a Claude.ai artifact with `mcp_servers: [{"type":"url","url":"https://mcp.monday.com/mcp","name":"monday"}]`, authenticated successfully and returned the full board list for `sabag-nadlan` (16+ boards, including board `5097553741` "Command Center | משימות, חסמים וקידום יומי" and board `5099714404` "🗂️ תיקים | Master").
Conclusion so far: the MCP server, the OAuth grant, and the account are healthy. The failure is isolated to tool injection in the Claude.ai chat surface.
Mission
A. Give the user a reliable, direct working path to monday.com from Claude Code itself. B. Confirm the root-cause isolation and produce a remediation checklist for the Claude.ai chat surface.
Tasks (execute in order, stop on unexpected state)

1. Register the MCP server in Claude Code:
bash

```bash
   claude mcp add --transport http monday https://mcp.monday.com/mcp
```

Then run `/mcp`, complete the OAuth flow for the `sabag-nadlan` account, and list the exposed tools. Record the exact tool names returned.

1. Read test: call `get_user_context` (or the closest board-listing tool actually exposed) and confirm the boards include `5097553741` and `5099714404`.
2. Write test (single, reversible): call `create_item` on board `5097553741` with item name: `🔧 TEST — חיבור Claude Code — מותר למחוק ידנית` Then read the item back by its returned id. Do NOT delete it, and do not touch any other item.
3. On any failure: capture the complete error (HTTP status, JSON-RPC error object, OAuth screen state), retry exactly once, then stop and report. Do not improvise destructive fixes.
4. Write `FINDINGS.md` containing:
   * Pass/fail table for steps 1–3 with exact error strings where relevant.
   * The actual tool list exposed by the server.
   * Remediation checklist for the Claude.ai chat surface, in this order: (a) Claude.ai → Settings → Connectors → monday.com → Disconnect, then reconnect and re-run OAuth; (b) verify the connector is enabled for the specific project "סבג נדל"ן"; (c) open a NEW chat, open the tools (sliders) menu next to the input box, confirm monday.com is toggled on before the first message; (d) retest with "תראה לי את הלוחות שלי"; (e) if tools are still absent, the evidence in this ticket supports a support report to Anthropic: installed-connector tools are not being injected into the chat tool index despite `installState: "connected"`.
Hard constraints (business operating rules — non-negotiable)

* Never delete anything in monday.com. Never modify existing items or financial data. Maximum ONE new test item, clearly labeled TEST.
* Never claim an action succeeded unless the API response confirms it. If information is missing — ask, don't invent.
Definition of done
Claude Code can list the boards AND has created and read back the single TEST item on board `5097553741` — OR a precise, evidence-backed failure report exists in `FINDINGS.md`.
