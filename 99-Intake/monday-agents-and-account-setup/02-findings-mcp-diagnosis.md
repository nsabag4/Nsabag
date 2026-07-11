# FINDINGS — monday.com MCP tools exposure

**Ticket:** monday.com MCP tools not exposed in Claude.ai chat surface
**Account:** `sabag-nadlan.monday.com` · **User:** נחי סבג (id `43672192`)
**MCP server:** `https://mcp.monday.com/mcp` (Streamable HTTP, OAuth)
**Executed from:** Claude Code (remote execution environment)
**Date:** 2026-07-04

---

## Summary

The monday.com MCP server, OAuth grant, and account are **healthy**. In **Claude Code**
the monday tools are fully exposed and working — the read test, board verification, and a
single reversible write test all **passed**. This reproduces and confirms the ticket's
root-cause isolation: **the failure is specific to the Claude.ai chat surface's tool
injection**, not to the server, the OAuth grant, or the account.

**Definition of done: met.** Claude Code listed the boards AND created + read back the
single TEST item on board `5097553741`.

---

## Pass/Fail table — Steps 1–3

| # | Step | Result | Evidence |
|---|------|--------|----------|
| 1 | Register MCP server + list exposed tools | ✅ PASS | Server already registered & OAuth-connected in this environment as `monday_com`. Tools resolved via tool search (full list below). |
| 1 | Read test — `get_user_context` | ✅ PASS | Returned user `43672192` (נחי סבג), account tier `pro`, 2 active members, 10 relevant boards. |
| 2 | Confirm target boards exist (`5097553741`, `5099714404`) | ✅ PASS | `get_board_info` returned both — see below. |
| 3 | Write test — `create_item` on `5097553741` | ✅ PASS | Item `3047758377` created. |
| 3 | Read item back | ✅ PASS | `get_board_items_page` returned item `3047758377`, name matches, all columns `null` (nothing else touched). |

**No failures. No retries needed. No errors captured.**

### Note on `get_user_context` board list vs. ticket

`get_user_context` returns only *frequently-visited* boards, so `5097553741` and
`5099714404` were **not** in that list — this is expected and not a failure. Both were
then confirmed to exist by direct `get_board_info` calls. Also note: `get_user_context`
lists a **different** board — `5097553973` — with a name nearly identical to the
Command Center board. The ticket's target `5097553741` is the correct, verified board
("Command Center | משימות, חסמים וקידום יומי"); `5097553973` is a separate similarly-named
board and was **not** touched.

### Boards confirmed (Step 2)

| Board ID | Name | State | Items | Workspace |
|----------|------|-------|-------|-----------|
| `5097553741` | Command Center \| משימות, חסמים וקידום יומי | active | 5 (before test) | SABAG 360 \| Work Management (`6602603`) |
| `5099714404` | 🗂️ תיקים \| Master | active | 0 | SABAG 360 \| Work Management (`6602603`) |

### TEST item created (Step 3)

| Field | Value |
|-------|-------|
| Item ID | `3047758377` |
| Name | `🔧 TEST — חיבור Claude Code — מותר למחוק ידנית` |
| Board | `5097553741` (Command Center) |
| URL | https://sabag-nadlan.monday.com/boards/5097553741/pulses/3047758377 |
| Created | 2026-07-04T21:31:02Z |
| Columns | all `null` — no column data written |

> ⚠️ This item is intentionally left in place (per ticket) and is safe to delete manually.
> No existing item, financial data, or other board was modified. Exactly one new item was created.

---

## Actual tool list exposed by the monday.com MCP server

The server exposed the following tools in this session (far more than the 8 the
Claude.ai connector registry advertised — including `get_user_context`, `get_board_info`,
`get_board_items_page`, `board_insights`, `get_board_activity`, and `get_updates` that the
companion skill docs referenced):

**Read / context**
- `get_user_context`
- `get_board_info`
- `get_board_items_page`
- `get_board_activity`
- `get_updates`
- `board_insights`
- `list_users_and_teams`
- `list_workspaces`, `workspace_info`
- `get_column_type_info`, `get_type_details`, `get_graphql_schema`
- `read_docs`, `get_assets`
- `search`
- `get_monday_dev_sprints_boards`, `get_sprints_metadata`, `get_sprint_summary`
- `get_automation_runs`, `get_automation_statistics`, `list_automations`

**Write / mutate**
- `create_item`, `change_item_column_values`
- `create_update`, `create_notification`
- `create_board`, `create_group`, `create_column`, `create_view`, `create_view_table`, `update_view`, `update_view_table`
- `create_dashboard`, `create_widget`, `all_widgets_schema`
- `create_doc`, `update_doc`
- `create_workspace`, `update_workspace`, `create_folder`, `update_folder`, `move_object`
- `create_form`, `update_form`, `form_questions_editor`, `get_form`, `create_form_submission`
- `create_automation`, `manage_automations`
- `create_workflow`, `update_workflow`, `plan_workflow`, `publish_workflow`
- `get_asset_upload_url`, `finalize_asset_upload`
- Agent tooling: `agent_catalog`, `manage_agent`, `manage_agent_skills`, `manage_agent_triggers`, `manage_agent_knowledge`
- Vibe: `vibe_ask`, `vibe_create`, `vibe_get`, `vibe_list`, `vibe_update`, `vibe_delete`, `vibe_publication`
- Generic API escape hatches: `all_monday_api`, `all_api_read`, `all_api_write`
- UI renderers: `show-table`, `show-chart`, `show-battery`, `show-assign`

> Tool names above are the server-native names. In Claude Code they are namespaced as
> `mcp__monday_com__<tool>` (e.g. `mcp__monday_com__get_user_context`).

---

## Remediation checklist — Claude.ai chat surface

Follow in order. The goal is to force the Claude.ai connector to re-inject the monday.com
tools into the chat tool index (the layer that is failing, per this evidence).

1. **Reconnect the connector.**
   Claude.ai → Settings → Connectors → monday.com → **Disconnect**, then reconnect and
   re-run the OAuth flow for the `sabag-nadlan` account. This mints a fresh token and
   forces the registry to re-advertise the tool set.

2. **Verify the connector is enabled for the specific project.**
   Open project **"סבג נדל״ן"** → project settings/connectors → confirm monday.com is
   enabled *for that project* (project-scoped connectors can be connected account-wide yet
   disabled inside an individual project).

3. **Toggle the connector ON in a NEW chat before the first message.**
   Open a **new** chat, click the **tools (sliders)** menu next to the input box, and
   confirm **monday.com is toggled on** *before* sending the first message. Tools selected
   mid-conversation are not always injected into an already-started turn.

4. **Retest with a natural-language prompt.**
   Send: **"תראה לי את הלוחות שלי"** ("show me my boards"). Expect the model to call a
   board-listing monday tool and return the `sabag-nadlan` boards.

5. **If tools are still absent → escalate to Anthropic support.**
   The evidence in this ticket supports a support report: installed-connector tools are
   **not being injected into the chat tool index** despite the connector-registry state
   showing `installState: "connected"` / `connected: true`. Include:
   - Registry state: `installedServerId ee0dc4b6-4c27-4eb6-98f3-dc5d5faf1e01`,
     `directoryUuid 49e0f9ba-7d45-4fb6-b098-55eec956fbc6`.
   - Proof the server + OAuth + account are healthy: this file (Claude Code reached the
     server, listed boards, and created/read item `3047758377` on board `5097553741`),
     plus the earlier Messages-API artifact test that returned 16+ boards.
   - The specific failure: deferred-tool search in Claude.ai chat returns only Google
     Workspace / Microsoft 365 tools; monday tools are absent from the index, and direct
     invocation of every name variant returns "Tool not found".

---

## Interim working path for the user

Until the Claude.ai chat surface is fixed, the reliable path to monday.com is **through
Claude Code** (this environment), where the full tool set is exposed and verified working.
Ask for monday actions here (list boards, read/create items, updates, etc.) and they will
execute against `sabag-nadlan` directly.
