# Wave 6 Final — Tier-3 + 6.D-α/β Follow-ups Design

**Date:** 2026-05-04
**Status:** Design + plan + self-audit consolidated. Execute immediately.
**Predecessors:** PR #426 (6.A) · PR #428 (6.B FTS5) · PR #429 (6.B Kanban) · PR #427 (6.C) · PR #430 (6.D)

---

## Goal

Close the five remaining items the user explicitly listed:

1. **Frontend pages** for Plugins + Models (PR #430 deferred backend-only)
2. **Mutation endpoints** for plugin enable/disable + main/aux model config
3. **Matrix reaction-based exec approval** (inbound `/sync` loop)
4. **MiniMax SkillSource adapter** (default-tap registration)
5. **Kanban dispatcher gateway loop** (Wave 6.B-β — sibling worker spawn on `kanban_create`)

Ship as four reviewable PRs. Each PR independently green on CI.

---

## Brainstorm — what shape does each item take?

### Item 1 — Frontend Plugins + Models pages

The kanban dashboard plugin works because hermes shipped `dist/index.js` (a pre-built React bundle). For Plugins + Models there's no equivalent prebuilt artifact in hermes — those pages live inside hermes' main React tree.

**Three options:**

A. Add a React build pipeline to OC. ~1-2 days; new toolchain (vite + tsx); changes how OC ships. **Reject — too invasive for this PR.**

B. Vanilla-JS HTML page that hits the existing JSON APIs. ~150 LOC per page; zero build step; aligned with OC's current single-`index.html` SPA shell. **Pick.**

C. Server-side render via Jinja2. Already a hard dep. ~100 LOC. Less interactive (no live filter without round-trip). **Reject — Plugins page benefits from client-side filtering.**

Pick **B**. New files: `static/plugins.html`, `static/models.html`, plus a small `static/_dashboard.js` shared utility. SPA shell `index.html` gets a header with three tabs: Chat / Plugins / Models.

### Item 2 — Mutation endpoints

Backend exists for read in PR #430. Add three POST routes:

- `POST /api/plugins/management/{id}/enable`
- `POST /api/plugins/management/{id}/disable`
- `POST /api/plugins/models/main` (body: `{"model": "..."}`)
- `POST /api/plugins/models/auxiliary` (body: `{"model": "..."}`)

**Auth gate:** `Bearer <_SESSION_TOKEN>` or `?token=` query param. Reject missing/wrong with 401. Mirror the `/api/pty` token compare.

**Persistence:** Both routes rewrite `~/.opencomputer/<profile>/profile.yaml` via `_atomic_write_yaml()`. Extract that helper from `cli_plugin.py` to a new `opencomputer/agent/profile_yaml.py` so dashboard + CLI share one writer.

**profile.yaml shape gotcha:** if the active profile uses `preset: <name>` instead of inline `plugins.enabled: [...]`, enabling/disabling has to:
1. Resolve preset → concrete list
2. Add/remove the id
3. Drop `preset:` and write `plugins.enabled:`

Document this clearly — surprising for users.

### Item 3 — Matrix /sync inbound + reaction approval

**Two halves:**

Half A — **/sync long-poll loop.** Matrix's Client-Server API exposes `GET /_matrix/client/v3/sync?since=<token>&timeout=30000`. The response includes new room events grouped by event type. We need to surface `m.reaction` events back to the agent runtime.

No `matrix-nio` dep available; OC's matrix adapter already uses raw httpx. Mirror that — start a `_poll_forever()` task in the adapter (modeled on telegram's). On each `m.reaction` event whose `m.relates_to` points at one of OUR sent messages, fire an approval-decision callback.

Half B — **Approval queue.** OC has no first-class approval primitive. Telegram already has `policy_notifier.py` that DMs the admin on `pending_approval` events. Reuse that **observation**: the matrix adapter posts a "want to run X?" message → records the (chat_id, event_id) → waits for a reaction with the right emoji.

Concretely:
1. Add `extensions/matrix/approval.py` — module exporting `request_approval(adapter, chat_id, prompt) -> asyncio.Future[bool]` that posts a message + registers the resulting event_id in a shared `_pending: dict[event_id, Future]`.
2. Sync loop checks each `m.reaction` event's `m.relates_to.event_id` against `_pending`; ✅ → resolve True, ❌ → resolve False; timeout → resolve False.
3. Wire as an `approval_callback` plugin extension point — picked up by whatever harness wants user approval (the `coding-harness` plugin's BashTool pre-flight is the canonical caller). For this PR we just expose the primitive; integrating callers can come later.

Scope discipline: **ship the primitive, ship the matrix /sync, document caller integration as next step.** Tying this directly into BashTool would balloon scope.

### Item 4 — MiniMax SkillSource adapter

**Survey result:** MiniMax-AI/cli source not on disk. Likely a github repo with a `/skills/` directory containing standard SKILL.md files.

**Pattern:** Generic GitHub-tree-backed adapter is already needed; `GitHubSource` exists (`opencomputer/skills_hub/sources/github.py`) but is identifier-driven, not org/repo-pinned.

Add `MiniMaxSource(SkillSource)` in `opencomputer/skills_hub/sources/minimax.py`:
- `name = "minimax"`
- Hardcoded `MiniMax-AI/cli` org/repo + `skills/` path
- `search()` lists skill dirs via GitHub Contents API
- `fetch()` downloads SKILL.md + listed assets via raw.githubusercontent.com
- `inspect()` parses just the YAML front-matter from SKILL.md

Register in `opencomputer/skills_hub/router.py`'s default tap list. Identifier prefix: `minimax/<skill-name>` → routes to this source.

**No GitHub token required** for public read (60 req/h unauthed; users with `GITHUB_TOKEN` env get 5000 req/h).

### Item 5 — Kanban dispatcher gateway loop

`dispatch_once()` already does the work — reclaim, promote, spawn. We just need to call it on a tick inside the gateway when `cfg.kanban.dispatch_in_gateway is true` (default).

**Implementation:** Add `_start_kanban_dispatcher_loop()` to `opencomputer/gateway/server.py`, modeled on the existing `_start_outgoing_drainer()` (line 301):
- Runs every 5s
- `kb.connect()` + `dispatch_once(conn, max_spawn=4)`
- Stops cleanly via `self._stop_event` (or new `_kanban_stop`)
- Logs spawned worker IDs

**Worker spawn function:** `dispatch_once` accepts a `spawn_fn(task, workspace_path)` callback. The default `_default_spawn` in `kanban/db.py` likely shells out to `oc kanban worker <task_id>`. The gateway dispatcher passes a closure that uses the gateway's existing process spawner so workers inherit the gateway's env (auth, profile, etc.). Verify the default first; only override if it's wrong.

---

## Plan (executable)

### PR-1 — Wave 6.D-α follow-ups: Frontend + Mutation endpoints

**Branch:** `feat/wave6da-frontend-mutations`
**LOC est:** ~600

**Tasks:**

1.1 Extract `_atomic_write_yaml()` to `opencomputer/agent/profile_yaml.py`. Update `cli_plugin.py` to import from the new util. Keep backward-compat re-export.

1.2 In `opencomputer/dashboard/plugins/management/plugin_api.py`, add:
- `POST /{plugin_id}/enable` — token-gated; reads `profile.yaml`, resolves preset to list if needed, adds id, atomic write, returns updated state.
- `POST /{plugin_id}/disable` — same but removes id.
- `POST /set-preset` — body `{"preset": "<name>"}` for users who prefer presets.

1.3 In `opencomputer/dashboard/plugins/models/plugin_api.py`, add:
- `POST /main` — body `{"model": "..."}`; updates `model.model` in `~/.opencomputer/<profile>/config.yaml` via atomic write.
- `POST /auxiliary` — body `{"model": "..."}`; updates `model.aux_model`.

1.4 Token gate: write a small `_require_session_token(request)` dependency in a shared `dashboard/_auth.py`. Both new POST surfaces use it.

1.5 New static pages:
- `opencomputer/dashboard/static/plugins.html` — table of plugins with toggle buttons that call enable/disable. Filter by name. Auth-status badges.
- `opencomputer/dashboard/static/models.html` — table of usage stats from `/api/plugins/models/usage`. Per-row "Set as main" / "Set as auxiliary" buttons.
- `opencomputer/dashboard/static/_dashboard.js` — shared helpers: token from URL, fetch wrapper, formatters.
- Update `opencomputer/dashboard/static/index.html` — add nav header (Chat / Plugins / Models).

1.6 Tests in `tests/test_dashboard_mutations.py`:
- POST without token → 401
- POST with bad token → 401
- POST enable → profile.yaml grows by id
- POST disable → profile.yaml shrinks
- POST main model → config.yaml updated
- Concurrent enable + disable → atomic (no torn write)

**Verification:**
- `pytest tests/test_dashboard_*.py tests/test_pty_bridge.py` — green
- Full suite — green
- `ruff check opencomputer/dashboard/ opencomputer/agent/profile_yaml.py tests/test_dashboard_mutations.py` — clean

### PR-2 — Wave 6.E.1: Kanban dispatcher gateway loop

**Branch:** `feat/wave6e-kanban-dispatcher-loop`
**LOC est:** ~250

**Tasks:**

2.1 In `opencomputer/gateway/server.py`, add `_start_kanban_dispatcher_loop()`:
- Reads `cfg.kanban.dispatch_in_gateway` (default true)
- Skips startup if false
- Tick interval: 5s (configurable via `cfg.kanban.dispatch_interval_seconds`)
- Per tick: open kanban DB, call `kb.dispatch_once(conn, max_spawn=4)`
- Logs `result.spawned`, `result.crashed`, `result.timed_out`
- Cancellable via `_stop_event`

2.2 Verify `_default_spawn` in `kanban/db.py` produces a workable subprocess (correct env, cwd, executable). If broken in OC's pip-installed layout, add a small shim.

2.3 Tests in `tests/test_kanban_dispatcher_loop.py`:
- Loop starts when config flag true; doesn't start when false
- Two tasks created → 2 worker rows after one tick (use mock spawn_fn)
- Loop stops cleanly on `_stop_event.set()`
- Crashed worker row reclaimed on next tick

**Verification:**
- `pytest tests/test_kanban_*.py` — green
- Full suite — green
- ruff clean
- Manual smoke: `oc kanban create` while gateway running → worker row appears

### PR-3 — Wave 6.E.2: MiniMax SkillSource adapter

**Branch:** `feat/wave6e-minimax-skill-source`
**LOC est:** ~300

**Tasks:**

3.1 Add `opencomputer/skills_hub/sources/minimax.py`:
- `class MiniMaxSource(SkillSource)`
- Hardcoded `OWNER = "MiniMax-AI"`, `REPO = "cli"`, `SKILLS_PATH = "skills"`
- `search()` — `GET https://api.github.com/repos/{OWNER}/{REPO}/contents/{SKILLS_PATH}`; filters dirs whose name contains `query`
- `fetch()` — `GET https://raw.githubusercontent.com/{OWNER}/{REPO}/main/skills/<id>/SKILL.md` + child files via Contents API recursion (cap depth at 3, file size at 1 MiB)
- `inspect()` — fetch SKILL.md, parse YAML front-matter only

3.2 Register in `opencomputer/skills_hub/router.py` default tap list.

3.3 Optional `GITHUB_TOKEN` env var → `Authorization: Bearer` header on requests.

3.4 Tests in `tests/test_skill_source_minimax.py`:
- Use httpx mock — no real network in CI
- search('foo') returns 0 entries when API returns []
- fetch('minimax/some-skill') downloads SKILL.md + 1 ref file
- inspect parses front-matter
- 404 from GitHub → returns None
- 403 rate-limit → logs warning, returns None
- Long path / size cap respected

**Verification:**
- `pytest tests/test_skill_source_*.py` — green
- Full suite — green
- ruff clean

### PR-4 — Wave 6.E.3: Matrix /sync inbound + reaction approval primitive

**Branch:** `feat/wave6e-matrix-reactions`
**LOC est:** ~600

**Tasks:**

4.1 Add `_poll_forever()` to `extensions/matrix/adapter.py`:
- Long-poll `GET /_matrix/client/v3/sync?since=<token>&timeout=30000` with httpx
- Persist `next_batch` token across reconnects (same pattern as telegram update_id)
- For each `rooms.join.<room_id>.timeline.events` entry of type `m.reaction`, dispatch to a registered handler

4.2 Add `extensions/matrix/approval.py`:
- `class ApprovalQueue` — manages `dict[event_id, asyncio.Future[bool]]`
- `request_approval(adapter, chat_id, prompt, *, allow_emoji="✅", deny_emoji="❌", timeout=300) -> Future[bool]`
- Sync-loop callback `on_reaction(event)` resolves the right Future

4.3 Plug-in extension point: register `ApprovalQueue` as a `gateway.approval_provider` so other tools (later) can call `await api.approval_provider.request("...")`. For this PR the queue is exposed but no built-in tool calls it yet — that's a follow-up.

4.4 Tests in `tests/test_matrix_sync.py` + `tests/test_matrix_approval.py`:
- `_poll_forever` parses a fixture sync response and emits reactions
- Approval future resolves True on ✅
- Approval future resolves False on ❌
- Approval future times out → False
- Wrong-event-id reaction → noop
- Disconnected /sync (httpx error) → exponential backoff, no crash

**Verification:**
- `pytest tests/test_matrix_*.py` — green
- Full suite — green
- ruff clean
- No matrix-nio import (still pure httpx)

---

## Self-audit (rigorous)

Critic mode on. Ten audit lenses applied to the plan above; refinements rolled in inline.

### A1. Silent API drift — VERIFIED before plan
- `dispatch_once()` signature confirmed by reading code: `(conn, *, spawn_fn=None, ttl_seconds=..., dry_run=False, max_spawn=None, failure_limit=...)`. Plan uses `max_spawn=4` correctly.
- `_atomic_write_yaml` confirmed to exist at cli_plugin.py:475. Public extraction needs a redirect re-export to avoid breaking importers.
- `SkillSource` ABC requires `name`, `search`, `fetch`, `inspect`. Plan has all four.
- Matrix C-S /sync API is stable v3 endpoint; payload shape verified against Matrix spec.

### A2. PR-1 surprise — preset → inline conversion
A user with `preset: coding` in profile.yaml clicking "disable kanban" expects kanban to be removed AND the preset to stay. But removing one id from a preset MUST inline-expand the list (you can't subtract from a preset by reference). Plan acknowledges this. **Refinement:** the API response includes a `preset_dropped: true` flag so the frontend can show "Switched from preset 'coding' to inline list because you customized it". Without that signal users get confused.

### A3. PR-1 race condition
Two browser tabs hit `enable A` and `disable B` at the same time. Both read profile.yaml → both write. Last write wins, one update is lost. **Refinement:** wrap read+modify+write in a `filelock.FileLock(profile_yaml.lock)`. OC already depends on `filelock>=3.16` per pyproject.toml.

### A4. PR-1 token leak in HTML
The session token is injected into every static page so client JS can attach it to fetches. If a user reloads the page after copying it, the token is in the rendered HTML — anyone shoulder-surfing or screen-sharing leaks it. **Refinement:** mask the token inside DOM (don't print it visibly), and rotate on `oc dashboard restart`. Also document: don't screen-share the dashboard URL with token query string. Acceptable on a single-user localhost host.

### A5. PR-1 model field name
Plan said `cfg.model.model` and `cfg.model.aux_model`. **Verify before writing.**  Need to check `opencomputer/agent/config.py` for the exact field names. Code uses `Model` dataclass — need actual field names. Will grep before writing.

### A6. PR-2 spawn_fn behaviour
The plan trusts `_default_spawn` to do the right thing. **Refinement:** read it before relying. If it shells out to `oc kanban worker`, that subcommand must exist; if to `python -m opencomputer.kanban.worker`, that module must exist. Plan added 2.2 already; reinforce: do this verification IN PR-2 task 2.2 and short-circuit out if broken.

### A7. PR-2 dispatcher startup ordering
The dispatcher loop must start AFTER the config-load phase but BEFORE plugins start scheduling tasks. Plan didn't specify ordering. **Refinement:** start it at the same point as `_start_outgoing_drainer()` (i.e. after `_fire_startup_pings`, before `serve_forever`). Mirror exactly.

### A8. PR-3 GitHub rate limits
60 req/h unauthenticated. A naive `search` that lists every skill on every keystroke will hit it in seconds. **Refinement:** add a 60-second TTL cache around the directory listing (Python `functools.lru_cache` won't expire — use a manual `dict[str, tuple[float, list]]`). Same for inspects.

### A9. PR-3 partial fetch — what counts as success
A skill with 12 ref files where the 8th 404s — do we return a partial bundle or None? **Refinement:** return None and log; user gets a clear "skill incomplete" rather than a half-installed skill that breaks at runtime. Document this.

### A10. PR-4 Matrix /sync security model
The /sync runs with the bot's access token. If the bot is in 50 rooms it'll receive every reaction in all of them. We only want reactions on messages WE sent. **Refinement:** the approval queue keeps a set of event_ids WE sent and ignores reactions on anything else. This already follows from the design (only registered futures resolve), but document as security property.

### A11. PR-4 reaction lookup performance
A long-running session with 1000+ pending approvals would have a big dict. Reasonable; not a problem in practice (each approval resolves in seconds).

### A12. PR-4 access token presence
The matrix adapter requires `MATRIX_ACCESS_TOKEN`. If unset, `_poll_forever` will get 401 forever. **Refinement:** check on startup; log a clear "matrix /sync disabled — MATRIX_ACCESS_TOKEN not set" and return early. Do NOT crash the gateway.

### A13. Cross-PR dependency — none
PRs 1, 2, 3, 4 are independent. Order: 1 → 2 → 3 → 4 (just for review cleanliness). If any one is red on CI, the others can still merge.

### A14. Honest deferrals (after this batch)

After all four PRs merge, the remaining open items (long-tail, intentionally not in this batch):
- BashTool integration with the matrix approval primitive (an integration step; the primitive itself ships in PR-4)
- Live event streaming on Plugins/Models pages (current pages are read-on-load, refresh button is fine for v1)
- Authenticated alpha-vantage / financial-datasets MCP setup (not in scope — separate work)

These are NOT shipped here and that's fine.

### A15. Failure modes / rollback
If PR-1 breaks the dashboard, users can `git revert <sha>` and the prior `/api/health`-only dashboard still works. None of these PRs rewrites a load-bearing core path; they all add new files or extend isolated modules.

---

## Final plan summary

| PR | Title | Branch | Files (new) | Files (modified) | Tests | LOC |
|---|---|---|---|---|---|---|
| 1 | Wave 6.D-α — Frontend Plugins/Models + Mutation endpoints | feat/wave6da-frontend-mutations | `agent/profile_yaml.py`, `dashboard/_auth.py`, `dashboard/static/plugins.html`, `dashboard/static/models.html`, `dashboard/static/_dashboard.js`, `tests/test_dashboard_mutations.py` | `dashboard/plugins/management/plugin_api.py`, `dashboard/plugins/models/plugin_api.py`, `dashboard/static/index.html`, `cli_plugin.py` | 8+ | ~600 |
| 2 | Wave 6.E.1 — Kanban dispatcher gateway loop | feat/wave6e-kanban-dispatcher-loop | `tests/test_kanban_dispatcher_loop.py` | `gateway/server.py`, `kanban/db.py` (maybe) | 4+ | ~250 |
| 3 | Wave 6.E.2 — MiniMax SkillSource adapter | feat/wave6e-minimax-skill-source | `skills_hub/sources/minimax.py`, `tests/test_skill_source_minimax.py` | `skills_hub/router.py` | 6+ | ~300 |
| 4 | Wave 6.E.3 — Matrix /sync + reaction approval primitive | feat/wave6e-matrix-reactions | `extensions/matrix/approval.py`, `tests/test_matrix_sync.py`, `tests/test_matrix_approval.py` | `extensions/matrix/adapter.py` | 8+ | ~600 |

**Total: ~1750 LOC across 4 PRs.**

---

## Execute now

After this doc lands on the design branch, I'll create the four feature branches in sequence, ship them as PRs, and merge each on green CI.
