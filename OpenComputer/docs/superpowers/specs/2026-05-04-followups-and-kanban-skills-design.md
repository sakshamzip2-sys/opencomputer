# Wave 6 Final Follow-ups + Kanban Skills Port — Design

**Date:** 2026-05-04
**Status:** Brainstorm + plan + audit consolidated. Execute immediately.
**Inspirations:** https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban + tutorial

---

## Goal

Close every honest deferral remaining from the Wave 6 finale + fill the
gap the Hermes kanban-docs review surfaced.

| Item | Title |
|---|---|
| A | BashTool + ConsentGate matrix approval integration |
| B | SSE event streams for Plugins/Models pages |
| C | MiniMax end-to-end smoke test (gated by env var) |
| E | kanban-worker + kanban-orchestrator skills (verbatim port) |

(Item D = the hermes-doc review itself, already complete — output
folded into Item E.)

Ship as four reviewable PRs. Each independently green on CI.

---

## Hermes-doc cross-check vs what OC has shipped

Quick inventory after reading the Hermes kanban docs (PR #429 is a
verbatim port of the kernel; here's the audit):

✅ Already shipped (PR #428 / #429 / #433):
- Core SQLite kernel + 7 statuses + 7 worker tools + system-prompt
- 15 CLI subcommands including `notify-subscribe/list/unsubscribe`
- `--idempotency-key` flag on `oc kanban create`
- Gateway-embedded dispatcher with circuit-breaker
- Dashboard plugin + REST + `/events` WebSocket
- `kanban-video-orchestrator` skill

❌ Missing — Hermes docs reference these:
- `kanban-worker` skill — teaches workers the lifecycle (call_show, do
  work, complete). Hermes ships at `skills/devops/kanban-worker/SKILL.md`,
  134 lines. **Item E.1.**
- `kanban-orchestrator` skill — patterns for decomposition + linking +
  delegation. Hermes ships at `skills/devops/kanban-orchestrator/SKILL.md`,
  152 lines. **Item E.2.**

The other Hermes-doc features (`/kanban` slash bypassing running-agent
guard, gateway auto-subscribe, multi-board boards switch UI, drag-drop
React UI, lanes-by-profile) are out-of-scope for this PR set — they
each warrant their own session. The two skills are the load-bearing
gap.

---

## Item A — BashTool + ConsentGate matrix approval

### Status before this PR
- `extensions/matrix/approval.py` ships `ApprovalQueue` + `request_approval()` (PR #436)
- The Matrix /sync loop drives the queue
- **No tool is wired to call into it** — the queue is dead code from a
  user perspective until something asks it for approval

### Where the integration lands
`opencomputer.agent.consent.gate.ConsentGate.set_prompt_handler` is the
existing extension point. The handler signature is:

```python
PromptHandler = Callable[[str, CapabilityClaim, "str | None"], Awaitable[bool]]
```

The handler returns True if a prompt was dispatched (gate then waits
for `resolve_pending`); False if no channel is bound (gate auto-denies).
Pattern matches the existing telegram approval-button flow.

### Plan

1. New `extensions/matrix/consent_bridge.py` exporting
   `make_matrix_prompt_handler(adapter, chat_id_resolver)` — returns a
   PromptHandler closure that:
   - Posts the approval prompt via `request_approval()`
   - Calls `gate.resolve_pending(session_id, capability_id, allowed=...)` when the future resolves
   - Returns True (prompt dispatched) immediately

2. The closure runs the await in a background task so the
   PromptHandler can return synchronously while waiting for the
   reaction. ConsentGate already has the per-(session, capability)
   pending-decisions registry that the resolution can write into.

3. `chat_id_resolver(session_id) -> str` is a small helper — for v0
   we read from a module-level setting (config.matrix.consent_chat_id);
   binding to per-session chats is a follow-up.

4. New `register_matrix_consent_handler(gateway, adapter)` helper
   wires the handler onto the gateway's ConsentGate. Caller-side wires
   it from `extensions/matrix/plugin.py` `register()` when the matrix
   adapter is configured AND the user opted in (config flag
   `matrix.consent_handler: true`, default off).

5. Per-tool consent claims already exist on BashTool via the F1
   ConsentGate (PR #64). No BashTool change needed — the gate is the
   integration point.

### Audit lenses
- A1 (silent API drift): handler signature is `(str, CapabilityClaim, str|None) -> Awaitable[bool]` — verified above
- A2 (chat_id resolution): for v0, hardcoded chat from config; per-session resolver is its own follow-up
- A3 (resolve_pending re-entrance): the resolution is fired from the matrix /sync loop's task, into ConsentGate's pending dict. ConsentGate already locks pending state; no race.
- A4 (timeout): if matrix is configured but the user doesn't react, the existing matrix request_approval timeout (300s default) resolves False, which `resolve_pending(allowed=False)` propagates to ConsentGate

### Test plan
- mock adapter + ConsentGate + assert prompt-handler returns True on dispatch
- adapter not connected → handler returns False
- approval future resolves True → ConsentGate.resolve_pending called with allowed=True
- approval timeout → resolve_pending called with allowed=False

---

## Item B — SSE streams for Plugins/Models pages

### Status before this PR
- Both pages are pull-only — clicking a button refreshes via REST GET
- A second tab editing profile.yaml doesn't reflect in the first tab

### Plan

1. New `opencomputer/dashboard/_sse.py` — small helper:
   `async def yaml_change_events(path: Path, interval: float = 1.0)` that
   yields a JSON event each time the file's mtime changes. Inotify
   isn't cross-platform reliable; mtime polling at 1s is the simplest
   correct thing.

2. `/api/plugins/management/stream` (FastAPI route) — `StreamingResponse`
   with `text/event-stream` media type. Watches profile.yaml, emits
   `event: list-changed\ndata: {...}\n\n` on every mtime bump. Token-gated.

3. `/api/plugins/models/stream` — same shape, watches `sessions.db`'s
   mtime so the analytics page refreshes when new sessions land.
   (sessions.db is updated frequently; we throttle to 5s).

4. JS side: new `OCDash.subscribeStream(url, onMessage)` helper using
   `EventSource`. Pages auto-reconnect on disconnect; on first
   connection we still hit the existing GET endpoint to populate
   initial state.

5. Pages add a small "🟢 live" or "⚪ stale" pill in the header.

### Audit lenses
- A1 (long-poll vs SSE): SSE is unidirectional, fits "server pushes when state changes". Long-poll would also work but doubles client-side complexity.
- A2 (resource leak): browser closes EventSource on tab close; FastAPI's StreamingResponse cancellation propagates the asyncio.CancelledError to the generator. Watch for that and break cleanly.
- A3 (auth): SSE inherits the same `?token=` query param the existing pages use. EventSource doesn't support custom headers, so query param is the only path.
- A4 (file-watch scaling): polling 1s on a single file is fine. We don't watch arbitrary paths.

### Test plan
- TestClient `client.stream("GET", url)` — reads N events from a fixture mtime change loop
- Bad token rejected
- Disconnect mid-stream → generator exits cleanly

---

## Item C — MiniMax real-network smoke test

### Status before this PR
- `MiniMaxSource` is unit-tested with fixture clones
- No test confirms the upstream `MiniMax-AI/cli` repo is reachable + has parseable SKILL.md files

### Plan

1. New `tests/test_minimax_real_network.py` — single integration test
   that does a real `git clone` of `MiniMax-AI/cli` and validates that
   `search()` returns at least one valid SkillMeta.

2. Gate via `@pytest.mark.network` + skip unless env var
   `OC_TEST_NETWORK=1`. CI doesn't set this so it's skipped by default.

3. Document in `tests/README.md` (or a top-of-file docstring) how to
   run the network tests locally:
   ```
   OC_TEST_NETWORK=1 pytest tests/test_minimax_real_network.py
   ```

### Audit lenses
- A1 (CI flakiness): network failures must NOT fail CI. Hence the env-var gate.
- A2 (rate limit): a single git clone per run is fine; GitHub allows ~60 unauth req/h before throttling.
- A3 (test fixture leak): use tmp_path, clean up after.

### Test plan
- Run locally with `OC_TEST_NETWORK=1` — verify it actually clones + finds skills
- Skipped without the env var
- Adapter still importable on hosts with no git installed (covered by existing fixture tests)

---

## Item E — kanban-worker + kanban-orchestrator skills

### Status before this PR
- Hermes ships `skills/devops/kanban-{worker,orchestrator}/SKILL.md`
- OC's PR #429 only ported `kanban-video-orchestrator` (the meta-pipeline skill)
- The two foundational skills that teach the lifecycle are missing

### Plan

1. Verbatim port (rename HERMES_* → OC_*):
   - `opencomputer/skills/kanban-worker/SKILL.md` (~134 lines)
   - `opencomputer/skills/kanban-orchestrator/SKILL.md` (~152 lines)

2. Validate against OC's skill validator (kebab-case names, ≤500-char descriptions, no reserved words).

3. Reference these skills from KANBAN_GUIDANCE — the system-prompt
   already says "see kanban-orchestrator skill" but nothing existed
   to back that pointer. This commit closes the dangling reference.

### Audit lenses
- A1 (validator failures): Hermes' descriptions may exceed 500 chars or use forbidden patterns. Trim during port if so.
- A2 (env var rename): HERMES_KANBAN_TASK → OC_KANBAN_TASK is the only widespread rename. `hermes kanban` → `oc kanban` for command examples.

### Test plan
- Existing skill discovery tests pick them up automatically
- One assertion in test_kanban_skills.py that both skills are discoverable and have valid SKILL.md frontmatter

---

## Final plan summary

| PR | Title | Branch | LOC |
|---|---|---|---|
| 1 | Wave 6.E.4 — BashTool + ConsentGate matrix approval | feat/wave6e-matrix-consent-bridge | ~300 |
| 2 | Wave 6.D-β — SSE streams for Plugins/Models pages | feat/wave6db-dashboard-sse | ~250 |
| 3 | Wave 6.E.5 — MiniMax real-network smoke test | feat/wave6e-minimax-network-test | ~80 |
| 4 | Wave 6.B-γ — kanban-worker + kanban-orchestrator skills | feat/wave6b-kanban-skills | ~290 (verbatim port) |

Total: ~920 LOC across 4 PRs. Execute now.
