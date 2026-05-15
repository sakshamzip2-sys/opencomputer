# MCP Server — Driving OC Approvals From External Clients

> **Status:** Shipped 2026-05-15. See `docs/plans/mcp-openclaw-port.md`
> (Milestone 3) for the design plan.

OC ships an MCP server (`opencomputer/mcp/server.py`) that exposes the
running profile's session history, channel send-path, and consent
surface to external MCP clients (Claude Code, Cursor, IDE plugins).
Run via `oc mcp serve`.

The M3 extension adds a long-poll subscription so external MCP clients
can drive OC's permission-prompt queue inline — useful when you're
working inside Claude Code and want to grant a pending OC consent
request without context-switching to a terminal.

---

## Enabling the approval surface

The approval long-poll tool is **OFF by default** because consent state
is sensitive — any MCP client connected to OC could enumerate pending
grants if the tool were exposed unconditionally.

To turn it on:

```bash
oc mcp serve --enable-approvals
```

The `--enable-approvals` flag adds the M3 long-poll tool
(`permissions_request_subscribe`) to the server's capability list.
Other tools (`permissions_list_open`, `permissions_respond`) were
already part of the Hermes-parity surface and remain available
regardless.

---

## The flow

1. **OC raises a consent request.** The agent (CLI / TUI / gateway)
   hits a capability that requires explicit permission. OC writes a
   row to `consent_requests` with `state='pending'`.
2. **External MCP client long-polls.** The client (e.g. Claude Code)
   has called `permissions_request_subscribe` with a long timeout.
   The call blocks inside the OC MCP server until either the
   `consent_requests` row arrives or the timeout elapses.
3. **Client renders + asks user.** The client UI surfaces the pending
   request to the user (e.g. Claude Code prints "OC is asking to
   write to /var/log/x — allow?").
4. **User responds via the client.** The client calls
   `permissions_respond` with the user's decision AND
   `granted_by="mcp_client"` so the audit log distinguishes it from
   CLI grants.
5. **OC unblocks.** The agent that raised the consent request sees
   the new grant and proceeds.

---

## Tools exposed

### `permissions_request_subscribe(timeout_s: float = 30.0, poll_interval_s: float = 1.0) -> list[dict]`

**M3 — only available with `--enable-approvals`.** Long-poll for
pending F1 consent requests. Returns the same shape as
`permissions_list_open` but blocks instead of returning immediately.

- `timeout_s` is capped at 120s. Clients wanting longer holds should
  call again after a return-empty.
- `poll_interval_s` is bounded `[0.05, 5.0]`. Sub-second polling is
  fine; the SQLite query is cheap (single indexed scan of the
  pending rows).
- Returns an empty list on timeout, never an error — the timeout case
  is normal (no pending requests during the wait).

### `permissions_list_open(limit: int = 50) -> list[dict]`

Returns the currently-open consent-request rows immediately (no
blocking). Useful for status displays.

### `permissions_respond(...)`

Writes a consent grant or revocation. New `granted_by` param (M3)
records the source for audit:

- `granted_by="user"` (default; back-compat) — interactive CLI/TUI.
- `granted_by="mcp_client"` — external MCP client over this server.
- `granted_by="gateway"` — channel adapter / inline-reply path.

Unknown values are rejected (`{"ok": false, "error": "granted_by must be ..."}`).
This is the audit-source allowlist — adding a new source requires an
intentional code change to `_VALID_GRANTED_BY` in
`opencomputer/mcp/server.py`.

The `granted_by` field also lands on the resulting
`consent_grants.granted_by` row so subsequent `consent_history`
queries surface the attribution.

---

## Example: Claude Code integration

Install OC's MCP server in Claude Code's settings:

```jsonc
// ~/.config/claude-code/settings.json
{
  "mcp": {
    "servers": {
      "opencomputer": {
        "command": "oc",
        "args": ["mcp", "serve", "--enable-approvals"]
      }
    }
  }
}
```

Then inside Claude Code:

- "What pending approvals does OC have?" → invokes
  `permissions_list_open` or `permissions_request_subscribe`.
- "Grant fs.write for /var/log/x" → invokes `permissions_respond`
  with `granted_by="mcp_client"`.

OC unblocks the pending tool call as soon as the grant lands. The
audit log (`consent_history` tool) shows the grant with
`granted_by="mcp_client"` so you can later differentiate it from
grants entered directly in OC's CLI.

---

## Security notes

- **Default OFF.** The long-poll subscription is the only NEW tool
  gated by `--enable-approvals`. Pre-existing tools
  (`permissions_list_open`, `permissions_respond`) remain available
  unconditionally — they were already part of the v2 surface.
- **Audit source attribution.** Every grant records who entered it.
  `consent_history` surfaces the `granted_by` column.
- **No bypass of F1.** The MCP surface DOES NOT bypass any consent
  policy. It calls the same `ConsentStore.upsert` / `ConsentStore.revoke`
  paths the CLI uses. The HMAC-chained audit log records every
  decision regardless of source.
- **Outbound message surface still requires care.** A malicious MCP
  client connected via stdio could call `messages_send` to dispatch
  via your gateway adapters. The `messages_send` tool was always
  available; consider whether it needs its own gate.

---

## Operational tips

- Run the server in a dedicated terminal (Claude Code or the IDE
  spawns it as a subprocess; you don't need to keep your own
  instance alive).
- The server runs against the **active profile**. Switch profiles
  with `oc -p <profile> mcp serve --enable-approvals`.
- Per-profile credentials apply — the server uses `_home()` to
  resolve the active profile's `sessions.db`.
- If you want approvals BUT not session-history access, the simplest
  defence is to install the server in a profile that has empty
  session history. The fine-grained tool gate is a future patch.

---

## Reference: deferred work

The plan (`docs/plans/mcp-openclaw-port.md` Milestone 3) calls out
`notifications/openclaw/permission/requested` as a follow-up to the
long-poll path. The MVP shipped (and documented above) is the long-
poll; push notifications are a future enhancement.
