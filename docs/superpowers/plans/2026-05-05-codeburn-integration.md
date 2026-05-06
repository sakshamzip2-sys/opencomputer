# Codeburn × Langfuse × OpenComputer integration

> 6-track plan-of-record (2026-05-05). Adds OpenComputer as a codeburn
> provider upstream, ports codeburn's optimize heuristics to `oc
> optimize`, surfaces codeburn as the rich-dashboard backend for `oc
> cost dashboard`, AND adds a langfuse observability plugin + langfuse
> eval-backend opt-in + one-command langfuse self-host stack.
> Brainstorm + audit baked in.

## 0. Problem framing

Codeburn (`getagentseal/codeburn`, MIT, TypeScript) is a token-usage
tracker that reads session data from disk for 18 AI coding tools and
reports cost / one-shot rate / yield / dashboards. **It is not an
eval system.** OpenComputer's `evals/` system stays as-is.

But codeburn has three things OC genuinely benefits from:

1. **The dashboard UX** OC's `oc cost show` is a static table; codeburn
   has a TUI with auto-refresh, per-project / per-model / per-activity
   rollups.
2. **The optimize heuristics** finds re-read files, low Read:Edit ratio,
   ghost skills, bloated CLAUDE.md, etc. — concrete waste-detectors that
   work off telemetry OC already records.
3. **Multi-tool unified view** if codeburn supports OC, you can see
   spend across Claude Code / Codex / Cursor / OC in one dashboard.

## 1. Data shape verification (pre-execute)

| Source | Path | Per-call detail | Session boundaries | Project (cwd) | Tool calls |
|---|---|---|---|---|---|
| OC sessions DB | `~/.opencomputer/<profile>/sessions.db` (SQLite) | Aggregated per session | Yes (id + started_at + ended_at) | Yes (`sessions.cwd`) | Yes (`messages.tool_calls` JSON) |
| OC LLM events | `~/.opencomputer/<profile>/llm_events.jsonl` | Yes — per call | No (no session_id field) | No | No |

**Hybrid load strategy** for codeburn provider: enumerate sessions from
SQLite, then for each session pull LLM events from JSONL within the
session's `[started_at, ended_at]` window and join. Tool usage comes
from `messages.tool_calls`. Cost comes from per-event `cost_usd`.

Confirmed with live data — `~/.opencomputer/default/llm_events.jsonl`
has 1,298 events with shape:
```
{"ts": "...", "provider": "anthropic", "model": "claude-opus-4-7",
 "input_tokens": N, "output_tokens": N, "cache_creation_tokens": N,
 "cache_read_tokens": N, "latency_ms": N, "cost_usd": N, "site": "agent_loop"}
```

## 2. Track A — codeburn provider for OpenComputer

**Repo**: getagentseal/codeburn (upstream PR).
**File**: new `src/providers/opencomputer.ts` (~250 LOC).
**Pattern**: blend openclaw.ts (multi-dir discovery + JSONL parse) with
opencode.ts (SQLite session enumeration).

### Implementation outline

```typescript
// 1. discoverSessions: enumerate ~/.opencomputer/{*/,}/sessions.db
//    Each profile (default/, work/, personal/) is its own sessions DB.
//    Project = sessions.cwd column (or "unknown" fallback).

// 2. createSessionParser: for each session row, find LLM events
//    in <profile>/llm_events.jsonl with ts ∈ [started_at, ended_at].
//    Tool list = union of decoded JSON tool_calls across messages.

// 3. yield ParsedProviderCall per LLM event with:
//    - sessionId = sessions.id
//    - project = sessions.cwd
//    - userMessage = first user message text in session (truncated 500 chars)
//    - tools = per-message tool_call names + bash commands
//    - cost from event.cost_usd OR fallback calculateCost()
```

### Acceptance

- `npx codeburn report` after install picks up OC sessions automatically.
- Per-project / per-model rollups show OC spend.
- `--provider opencomputer` flag works (filtering).
- README updated with OC row in supported-providers table.

### Open as upstream PR

Branch off `main` of codeburn fork. Title: "feat(providers): add OpenComputer".

## 3. Track B — `oc optimize` command

**File**: new `opencomputer/cli_optimize.py` + wire into `cli.py`.
**Storage**: same OC data sources (sessions.db + llm_events.jsonl + on-disk skills/agents/CLAUDE.md inventory).

### Heuristics (priority-ordered by codeburn's own ranking)

| # | Finding | Detection rule | Estimated saving |
|---|---|---|---|
| 1 | **Files re-read across sessions** | Query `messages` where `name='Read'` (decoded from tool_calls) for same `path` arg in different sessions | sum(read sizes × N reads − 1 read worth) |
| 2 | **Low Read:Edit ratio** | Per-session: count Read calls vs Edit calls. Flag sessions with < 1:1 ratio | session retry-cycle estimate |
| 3 | **Ghost skills** | Skills in `~/.opencomputer/skills/` never invoked (cross-ref skill name vs `tool_calls` Skill invocations across all sessions) | tool-schema overhead × session count |
| 4 | **Ghost agents** | Same pattern for `~/.opencomputer/agents/` vs `Delegate`/`Agent` invocations | -- |
| 5 | **Unused MCP servers** | Query MCP registry (`oc mcp list`) vs mcp_call entries in tool_usage table. Flag servers with zero invocations in last 30 days | tool-schema bytes × tokens/byte × session count |
| 6 | **Bloated context files** | size of `~/.opencomputer/<profile>/SOUL.md`, `USER.md`, `MEMORY.md` vs reasonable threshold (e.g., > 8KB) | (size − 8KB) × session count |
| 7 | **High cache-write-to-read ratio** | sum(cache_creation_tokens) / sum(cache_read_tokens) per profile across last 7d. Flag if > 1:5 (most cache writes never get a hit) | cache_creation_tokens × $1.25/Mtok × inefficiency factor |
| 8 | **Cron junk** | (low priority) cron jobs in `~/.opencomputer/cron/jobs.json` that never produced output in last 30d | -- |

### CLI surface

```
oc optimize                    # default: scan last 30 days
oc optimize -p today           # today
oc optimize -p week            # last 7 days
oc optimize --top 10           # show top N findings only
oc optimize --json             # JSON for piping
oc optimize --grade            # one-line A-F setup health grade only
```

### Output format

Each finding displays: rank, category, estimated saving (tokens + USD),
detail string, copy-paste fix (when applicable).

```
1. [HIGH] Re-read file: /Users/saksham/CLAUDE.md
   → Read 47 times across 32 sessions, ~12 KB each
   → Save: ~564 KB ≈ 12K input tokens ≈ $0.04/30d
   → Fix: Add `~/.opencomputer/cache_paths` to declare it as
     cache-pinned (PR #475 follow-up)

2. [MED] Ghost skill: persona-classifier
   → 0 invocations in last 30 days
   → Save: ~250 tokens × ~30 sessions = 7.5K tokens/30d
   → Fix: oc skills disable persona-classifier
```

### Acceptance

- Returns A-F grade based on weighted findings.
- Findings ranked by `impact_tokens * urgency_factor`.
- `--json` output is machine-parseable.
- Run on Saksham's actual install — at least 3 real findings surface.

## 4. Track C — `oc cost dashboard` (codeburn shell-out)

**File**: extend `opencomputer/cli_cost.py` with a `dashboard` subcommand.

### Behavior

```
oc cost dashboard                    # opens codeburn TUI for OC sessions
oc cost dashboard --period today
oc cost dashboard --period 7days
oc cost dashboard --no-codeburn      # force native fallback
```

### Strategy

1. If `codeburn` is on PATH and supports `--provider opencomputer` (post-Track-A), shell out:
   `codeburn report --provider opencomputer ${period_args}`
2. If `codeburn` is on PATH but pre-Track-A version: shell out with no provider filter (all sessions including OC's openclaw-named JSONL if any).
3. If `codeburn` is NOT on PATH: print one-liner install hint:
   ```
   For richer cost analytics, install codeburn:
     npm install -g codeburn
     # or: brew install codeburn
   Then run: oc cost dashboard
   ```
   Plus fall back to existing `oc cost show` table.

This keeps OC's own command surface stable but opens the door to the
better tool when available. No vendoring of codeburn's TS code.

### Acceptance

- Without codeburn installed: `oc cost dashboard` prints install hint + falls back to native table. Exit 0.
- With codeburn installed: `oc cost dashboard` launches codeburn TUI.
- `--no-codeburn` always uses native fallback.

## 5. Self-audit

> Critic-as-expert pass.

**Q1. "Is the LLM-events-without-session-id matching robust?"**
Risk: events with `ts` outside any session window get dropped or attributed wrong. Mitigation: (a) use `[started_at-1s, ended_at+1s]` overlap window; (b) for orphan events (no session match), bucket as `unknown_session`. Acceptable lossy attribution for now.

**Q2. "Will Track A's PR get merged upstream? If not, do tracks B/C still work?"**
Track A is an upstream PR — codeburn maintainers may take days/weeks. Tracks B and C don't depend on Track A merging — Track B is pure OC work, Track C just shells out (works for ANY codeburn version that supports OC, including a local-fork install).

If upstream rejects, fork+publish-with-prefix (`@saksham/codeburn-oc`) as a fallback. But MIT-licensed and follows their "single file" pattern — high acceptance odds.

**Q3. "Track B duplicates codeburn — is this 'not invented here'?"**
No. OC's data lives in OC's storage; pulling it into a separate JS process to analyze is awkward when we could analyze it natively. Track B is *inspired by* codeburn's heuristics, ported to Python, operating on OC's schema. Codeburn for cross-tool dashboard; `oc optimize` for OC-specific workflow tightening. Different audiences.

**Q4. "Does Track B need session-correlated git data (yield)?"**
Codeburn's "yield" feature correlates sessions with git commits. OC's sessions table has `cwd` — enough to find the relevant repo. Implementing yield is real work (~1 day on top). Defer to v2 — not in scope for this plan.

**Q5. "Won't shelling out to codeburn from Track C feel hacky?"**
The alternative is a native rich-dashboard port (~3-5 days). Shell-out is 1 hour. Once Track A is upstream, shell-out IS the right answer because codeburn's dashboard improvements automatically flow to OC users without any porting. Native is the wrong layer.

**Q6. "What about privacy? Codeburn reads session content."**
Codeburn already reads session JSON for 18 tools. OC's data adds nothing new. Per-message text is truncated to 500 chars in `userMessage` field (matches openclaw provider). All processing local — no network. Document in PR.

**Q7. "Track B heuristics rely on tool_calls JSON parsing. What if the JSON is malformed?"**
Wrap each `json.loads(row.tool_calls)` in try/except; on failure, log + skip that message. Don't bring down the optimize run on one bad row.

**Q8. "What's the rollback story for Track A's upstream PR if it breaks codeburn for other users?"**
The provider list is opt-in via auto-detection — codeburn only picks up OC if `~/.opencomputer/` exists. No other users affected. PR is purely additive.

**Q9. "Can I test Track A without publishing?"**
Yes — clone codeburn locally, add the file, `npm link`, then `codeburn report`. Iterate locally, then PR.

**Q10. "Is `oc optimize` worth shipping as one PR?"**
8 heuristics is a lot. Could split into MVP (top 3) + follow-ups. But each is ~30 LOC of detection logic; bundling reduces review thrash. Ship together.

**Q11. "Test coverage for Track B?"**
Each heuristic gets one happy-path + one no-data-found test. Run on a synthetic SQLite fixture. ~150 LOC of tests.

**Q12. "Track C's --no-codeburn flag — necessary?"**
Yes. Tests need it (CI doesn't have codeburn). Also future-proof against codeburn breaking changes.

## 6. Track L1 — langfuse observability plugin

**Repo**: OpenComputer (this repo).
**File**: new `extensions/langfuse/` plugin (~250 LOC).
**Triggers when**: `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` env vars are both set; otherwise plugin loads as inert (registers nothing).

### Design

- Subscribes to `LLMCallEvent` emission via the existing observability sink.
- For each call, calls `langfuse.trace()` + `langfuse.generation()` with model, tokens, cost, latency, prompt/response (truncated).
- Lazy-imports `langfuse` at first event so users without it installed pay zero cost.
- Sends asynchronously via langfuse's built-in batching SDK — no extra latency on the agent loop.
- `LANGFUSE_BASE_URL` env defaults to `https://cloud.langfuse.com`; user can point at self-hosted.

### Acceptance

- Plugin loads cleanly when langfuse env vars unset (no errors in `oc doctor`).
- With env vars set + langfuse SDK installed, runs of `oc chat` emit traces visible in langfuse UI.
- 0ms regression on agent-loop hot path (verified with timing test).

## 7. Track L2 — `oc eval --backend langfuse`

**Files**: extend `opencomputer/cli_eval.py` + new `opencomputer/evals/langfuse_backend.py` (~150 LOC).
**Default**: existing local-runner backend; opt-in via `--backend langfuse` or `OPENCOMPUTER_EVAL_BACKEND=langfuse` env.

### Behavior

```
oc eval run reflect --backend langfuse
  ↓
1. If LANGFUSE_* env vars unset OR langfuse SDK not importable: error with helpful message.
2. Read existing JSONL cases.
3. Ensure langfuse dataset 'opencomputer-reflect' exists (create + populate items if missing — idempotent).
4. Define adapter function = existing site adapter wrapped to match langfuse's task signature.
5. Call dataset.run_experiment(name=..., task=adapter).
6. Print run URL + summary stats from langfuse response.
```

### Migration

- First time `oc eval run <site> --backend langfuse` runs, it bootstraps the langfuse dataset by uploading every JSONL case as a dataset item.
- Subsequent runs use the existing dataset — no re-upload.
- Cases never deleted from langfuse via OC; manual curation via langfuse UI.

### Acceptance

- `oc eval run reflect --backend langfuse` completes end-to-end against local langfuse.
- Run URL is printable.
- Falls back gracefully if langfuse unreachable.

## 8. Track L3 — `oc langfuse up/down` + docker-compose template

**Files**: `opencomputer/integrations/langfuse/docker-compose.yaml`, `opencomputer/integrations/langfuse/.env.template`, new `opencomputer/cli_langfuse.py` (~100 LOC).

### Compose stack

5 services exposed at `localhost:3000`:
- `langfuse-server` (Next.js web app + API)
- `langfuse-worker` (background eval/trace processor)
- `langfuse-postgres` (metadata)
- `langfuse-clickhouse` (high-volume traces)
- `langfuse-redis` (cache + queues)

Bind-mount `~/.opencomputer/langfuse/` for persistence across `up`/`down` cycles.

### CLI surface

```
oc langfuse up                 # docker compose up -d using OC's bundled template
oc langfuse down               # graceful stop (data preserved)
oc langfuse status             # health probe of langfuse-server :3000/api/health
oc langfuse logs               # tail compose logs
oc langfuse keys               # print/setup API keys (uses langfuse-server's first-run flow)
```

### Acceptance

- `oc langfuse up` succeeds on a clean machine with Docker installed.
- After `up`, `oc langfuse status` returns 200 OK from `/api/health`.
- `oc langfuse keys` prints the public + secret keys (or instructions for first-run).
- `oc langfuse down` cleanly stops; running `up` again resumes data.

## 9. Execution order

1. Track A — write `src/providers/opencomputer.ts` in `/tmp/codeburn`. Run locally to verify (npm link). Open upstream PR if hooks permit, otherwise leave the file as a deliverable diff for Saksham.
2. Track B — implement `cli_optimize.py` + heuristics + tests in OC. Wire into CLI.
3. Track C — extend `cli_cost.py` with `dashboard` subcommand.
4. Track L1 — implement `extensions/langfuse/` observability plugin.
5. Track L2 — implement `evals/langfuse_backend.py` + wire `--backend langfuse`.
6. Track L3 — write docker-compose template + `cli_langfuse.py`.
7. Run full pytest + ruff.
8. Commit + push on `fix/seedicon-deferred-finale-v2` branch.
9. Open OC PR.

## 7. Acceptance criteria

- ✅ `npx codeburn report` (with my Track A patch applied locally) shows OC sessions with correct token + cost data.
- ✅ Upstream PR opened to getagentseal/codeburn with green CI.
- ✅ `oc optimize` runs against Saksham's install and surfaces ≥ 3 real findings.
- ✅ `oc cost dashboard` works without codeburn (falls back to native) AND with codeburn (launches TUI).
- ✅ Full pytest passes.
- ✅ Plan + brainstorm + audit doc committed.

## 8. Out of scope

- Codeburn's "yield" feature (git correlation) — deferred.
- Codeburn's "compare" feature (model A/B benchmarking) — `oc eval` already covers benchmarking.
- Native port of codeburn's TUI (Track C alternative) — wrong layer; defer indefinitely.
- Auto-classifying OC sessions into the 13 codeburn task categories — codeburn already does this for any provider with sufficient data; once Track A is in, the categories show automatically.
