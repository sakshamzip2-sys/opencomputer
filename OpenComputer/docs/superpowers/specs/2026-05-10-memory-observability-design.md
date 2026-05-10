# Memory observability design

Date: 2026-05-10
Status: approved (post-Phase-4 audit)
Author session: parent repo `/Users/saksham/Vscode/claude/`

## Problem

OpenComputer's `MemoryManager` performs silent inline compaction when MEMORY.md or USER.md
exceed their character caps (4000 / 2000). The compaction helper at
`opencomputer/agent/memory.py:150` (`_compact_under_cap`) drops the oldest paragraph(s) and
prepends a `## Older notes (N entries compacted on YYYY-MM-DD)` header, but:

1. The agent gets no real-time signal when an entry is dropped. `MemoryWriteEvent`
   (`plugin_sdk/ingestion.py:288`) carries `content_size` only, not `compaction_delta`.
2. `MEMORY.md` / `USER.md` are frozen into every system prompt at session start, so a
   silent drop of a load-bearing rule (e.g. `failure-recovery-ladder` from 2026-04-29) is
   invisible until the next session starts and the prompt is missing it.
3. There is no scheduled or on-demand consolidation surface. The agent's `Memory` tool
   exposes `add` / `replace` / `remove` / `read`; in practice `remove` rarely fires.
4. Live `USER.md` is at 89% capacity with three drift categories (memo §4): time-bound
   entries masquerading as durable facts, unresolved status flags, and stale active-projects
   list.
5. ~~`memory.py:58` references `opencomputer memory prune` in an error message; no such CLI
   command exists.~~ **CORRECTION (during M3 execution, 2026-05-10):** `oc memory prune`
   IS a real command at `cli_memory.py:181`. The phantom-ref claim from the memo
   reconnaissance was wrong. Gap D is dropped from scope.

   Separately, `oc memory doctor` ALREADY exists at `cli_memory.py:629` as a multi-layer
   health command (baseline / episodic / docker / honcho / provider / dreaming /
   active_memory / vector_retrieval). It does NOT do per-paragraph audit. The new
   capability needed is per-paragraph inspection of MEMORY.md / USER.md, separate from
   the layer-health doctor.

The wire bus and event broadcast machinery to surface compaction events to TUI / dashboard
already exists (`opencomputer/gateway/wire_server.py` `WireEvent` ring buffer).

## Non-goals

- Periodic forced consolidation (cron/scheduler). Punted to Phase 2 follow-up; depends on
  `oc memory doctor` existing.
- TUI memory panel. Dogfood-gated per CLAUDE.md §5; depends on actual TUI symptom data.
- Splitting USER.md into PROJECTS.md + USER.md. BC break; symptoms can be addressed by the
  doctor CLI.
- Drop-next preview (cut by YAGNI sweep in Phase 2 audit).
- LLM-driven drift detection inside `oc memory doctor`. Doctor surfaces deterministic
  structure (cap %, age, length, TODO markers); judgment stays with user in `--interactive`.

## Architecture

```
        write path                                      read/audit path
        ──────────                                      ───────────────

   tools/memory_tool.py
          │                                          opencomputer memory doctor
          ▼                                                   │
  agent/memory.py:_append                                     ▼
    │      │                                       agent/memory_doctor.py
    │      │                                                  │
    │      ▼                                                  ▼
    │   _compact_under_cap ──── returns ──── (text, dropped_count)
    │                                                  │
    ▼                                                  │
  agent/memory_cap.py:_cap_status (NEW)                │
    │     │                                            │
    │     │ post-write CapStatus                       │
    │     ▼                                            │
    │   warning string ─── prepend to ToolResult.text  │
    │                  │                               │
    │                  └─ also stderr via logger       │
    ▼                                                  │
  _publish_memory_write_event(action, target,          │
                              content_size,            │
                              compaction_delta, ←─── new
                              dropped_paragraphs)←─── new
    │
    ▼
  plugin_sdk MemoryWriteEvent (frozen dataclass; new fields default 0)
    │
    ├──→ default_bus → MemoryBridge → provider.on_memory_write()
    └──→ default_bus → wire_server WireEvent broadcast (auto)
```

## Components (new + modified)

### NEW: `opencomputer/agent/memory_cap.py`

```python
@dataclass(frozen=True, slots=True)
class CapStatus:
    file_name: str          # "MEMORY.md" / "USER.md"
    bytes_used: int
    bytes_limit: int
    pct: float              # 0.0–1.0+ (overflow possible mid-compaction)
    paragraph_count: int

def cap_status(text: str, limit: int, file_name: str) -> CapStatus: ...
def warning_for(status: CapStatus, *, dropped: int = 0) -> str | None:
    """Return None if pct < 0.80 and dropped == 0."""
```

Pure module. Testable in isolation. No imports from `opencomputer.*`.

### MODIFIED: `opencomputer/tools/memory_tool.py`

After every successful `add` / `replace` / `remove`, compute post-write `cap_status`.
Prepend `warning_for(...)` to `result.content` when non-None (note: ToolResult uses
`content`, not `text`). Mirror to stderr via existing `logger.warning("[memory:warn] %s",
warning_text)`.

### MODIFIED: `opencomputer/agent/memory.py`

**No internal signature changes.** Drop count is recovered without touching
`_compact_under_cap` or `_compact_replace_under_cap` (both have direct-equality tests at
`tests/test_memory_md_cap_pressure.py:42`):

- In `_append` and `_replace`: before write capture
  `prior_count = _extract_prior_compaction_count(existing)`. After write capture
  `new_count = _extract_prior_compaction_count(new_text)`. Round drops =
  `new_count - prior_count`. Bytes-saved delta = `len(existing) + len(new_block) -
  len(new_text)` (or 0 if no compaction).
- `_publish_memory_write_event(...)` accepts `compaction_delta=0` and
  `dropped_paragraphs=0`.
- ~~Error-message reference at `memory.py:58` updated from `opencomputer memory prune` to
  `opencomputer memory doctor` (real command after M3).~~ Dropped during M3 (2026-05-10):
  `opencomputer memory prune` IS a real command at `cli_memory.py:181`. Phantom-ref claim
  was wrong; no fix needed.

### MODIFIED: `plugin_sdk/ingestion.py`

`MemoryWriteEvent` adds two fields with defaults:

```python
compaction_delta: int = 0       # bytes freed by this write's compaction (0 if no compact)
dropped_paragraphs: int = 0     # number of paragraphs dropped by this write
```

Frozen-dataclass-with-defaults is BC per `plugin_sdk/CLAUDE.md` §1.4.

### NEW: `opencomputer memory audit` subcommand in `cli_memory.py`

```
oc memory audit                  # MEMORY.md by default
oc memory audit --user           # USER.md only
oc memory audit --all            # both files
oc memory audit --interactive    # walk + prompt per paragraph
```

(Renamed from `doctor` — that name is taken by the multi-layer health command
at `cli_memory.py:629`.)

Read-only by default. Per paragraph: index, char count, flag annotations
(`[TODO]`, `[stale-status]`, `[possible-duplicate]`, `[long]`).
`--interactive` adds `[k]eep / [d]elete / [r]eplace / [s]kip` prompts. Delegates writes
to existing `MemoryManager.remove_*` / `replace_*` paths so locking, backup, and event
publication chain are reused (same write path the Memory tool uses).

## Data flow on overflow

1. `tools/memory_tool.py` calls `MemoryManager.append(...)`.
2. `MemoryManager._append` computes `new_text = existing + new_entry`. Over limit.
3. `_compact_under_cap(existing, new_entry, limit)` drops oldest paragraph(s) until fit.
   Returns the compacted text. (Drop count is recovered out-of-band by diffing the
   `## Older notes (N entries...)` cumulative counter — keeps `_compact_under_cap`'s
   existing return signature so `tests/test_memory_md_cap_pressure.py:42` continues to pass.)
4. Atomic write. `.bak` snapshot taken.
5. `_publish_memory_write_event(action="append", target="MEMORY.md",
   content_size=len(compacted_text), compaction_delta=len(existing) -
   len(compacted_text) + len(new_entry), dropped_paragraphs=2)` published to
   `default_bus`.
6. Subscribers: `MemoryBridge` → `provider.on_memory_write`; wire-server broadcast →
   future TUI/dashboard panels.
7. `tools/memory_tool.py` re-reads file size, builds `CapStatus`, calls
   `warning_for(status, dropped=2)`. Returns:

```
🛑 MEMORY MEMORY.md COMPACTED — DROPPED 2 ENTRIES (post-write 87%, 3480/4000 chars).
Run `oc memory doctor` to review what was kept; check `.bak` or `git log` for what was
dropped.

<original tool result text>
```

8. Stderr also gets `[memory:warn] MEMORY.md 87% (3480/4000 chars) dropped=2`.

## Error / failure handling

- `_post_write_warning` errors → swallow with `try/except`, return None. Memory write
  must not fail because of an observability layer. Same shape as
  `_publish_memory_write_event`'s `except Exception: pass` (`memory.py:433`).
- `oc memory audit --interactive` aborted via Ctrl-C mid-walk → no partial writes (each
  paragraph's decision is committed as it's made; user can break between paragraphs).
- File missing → audit prints `MEMORY.md is empty (0 chars / N cap)` and exits cleanly.

## Tests

- `tests/test_memory_cap.py` (15 tests) — `CapStatus` boundary tests (0%, 79%, 80%, 90%,
  101%); `warning_for` returns None below 80%; escalates on `dropped > 0` regardless of
  pct; USER.md vs MEMORY.md naming; singular/plural drops.
- `tests/test_memory_tool_warning.py` (7 tests) — Memory tool calls produce warnings at
  correct thresholds; warning is in result.content not the file body; error path
  unaffected; remove that drops below threshold no-warns.
- `tests/test_memory_event_compaction.py` (5 tests) — `MemoryWriteEvent.compaction_delta`
  populated on overflow; zero on non-overflow; BC test confirms old-shape construction
  still works; tool-warning escalates to COMPACTED variant on actual drop.
- `tests/test_memory_audit_cli.py` (7 tests) — read-only audit lists paragraphs with
  indices, flags TODO markers, supports `--user` / `--all`, includes cap pct, exits
  cleanly on missing files.
- `tests/test_memory_audit_interactive.py` (6 tests) — keep/skip/delete/replace/unknown
  via stdin; works on USER.md too.

## Migration / BC

- Plugin authors / providers consuming `MemoryWriteEvent` see two new fields with default
  0. No code changes required.
- Tool callers checking `result.text == "..."` exactly: M0 inventory pass identifies
  these; updated to `result.text.endswith(...)` or `"<original>" in result.text`.
- Phantom `opencomputer memory prune` ref becomes a real command — anyone scripting
  against the error message gets a working CLI now.

## Rollout

Single PR, milestones M1→M5 in order. Each milestone is an internal commit; squash on
merge. Dogfood pass in M5 against `~/.opencomputer/<profile>/{MEMORY.md, USER.md}` —
specifically resolves memo §6 follow-ups (anime entry, token-rotation flag, project list).

## Open items

- Phase 2 follow-up: periodic forced consolidation (memo §3 fix shape) — once `oc memory
  doctor` exists, the cron just calls it on a cadence with `--interactive` deferred.
- ~~TUI memory panel — blocked on TUI symptom report from a separate session.~~
  **CLOSED Tier-C (2026-05-10):** `EVENT_MEMORY_WRITE` + `MemoryWritePayload` schema
  added to `gateway/protocol(_v2).py`. `WireServer` subscribes to `default_bus` for
  `MemoryWriteEvent` on `start()` and broadcasts to all connected WS clients via new
  `_session_clients_all`. Ink+React `MemoryPanel` (`ui-tui/src/components/memoryPanel.tsx`)
  renders a single status line under the chat header. The "fictional auto bridge"
  diagram-claim from this spec's §Architecture line "default_bus → wire_server WireEvent
  broadcast (auto)" is now real.
- USER.md split (PROJECTS.md + USER.md) — re-evaluate after dogfood pass shows whether the
  doctor is sufficient.

## Tier-C postscript (2026-05-10 audit)

Audit of the M2 publisher path discovered three downstream defects beyond the original
spec's scope:

1. **`MemoryBridge._on_memory_write_event`** at `agent/memory_bridge.py:353-375` called
   `provider.on_memory_write(action, target, content_size)` — dropped
   `compaction_delta` and `dropped_paragraphs`. The `MemoryProvider.on_memory_write`
   signature in `plugin_sdk/memory.py:187-204` predated M2 and was never extended.
   **Status: closed Tier-B (2026-05-10).** Signature extended with kwargs (BC via defaults);
   bridge uses `inspect.signature` for per-provider kwarg projection so legacy 3-kwarg
   overrides keep working unchanged.
2. **Dashboard SSE projection** at `dashboard/routes/events.py:42-51` stripped every
   subclass-specific field — only the 6 base `SignalEvent` fields crossed. `compaction_delta`
   etc. never reached a browser SPA panel via that channel. **Status: closed Tier-A
   (2026-05-10).** Projection extracted as `project_event` module-level function; uses
   `dataclasses.asdict` to surface every field; failure-isolated with WARN-log fallback;
   privacy contracts pinned by tests for the 3 most sensitive event types.
3. **Spec's "auto bridge" claim** (§Architecture line 82). Tier-C makes it true.
   **Status: closed.**

## Tier-C+ postscript (2026-05-10 follow-through)

Audit against the Hermes TUI reference doc surfaced two user-visible gaps:

1. **No initial state on connect.** A wire client (TUI / dashboard SPA) connecting fresh
   sees nothing in the memory panel until the first `memory.write` event fires — the
   bus has no replay for global-broadcast events. **Status: closed.** New
   `METHOD_MEMORY_STATUS = "memory.status"` wire RPC + typed `MemoryStatusResult` schema
   in `protocol_v2`; `WireServer._collect_memory_status` reads MEMORY.md + USER.md via
   `MemoryManager` paths/limits and returns one `MemoryStatusEntry` per file. New
   `GET /api/v1/memory/status` REST endpoint mirrors the wire RPC for the dashboard SPA
   (which uses HTTP, not WS). Both surfaces share the same response shape + failure
   isolation (missing file → zero-size entry; unreadable file → omitted + WARN log;
   no `MemoryConfig` → empty entries, never an error). The TUI calls `memoryStatus()`
   after `hello()` to seed `MemoryPanel` state from first frame.
2. **Panel only tracked the last-written file.** When MEMORY.md was written then USER.md
   was written, MEMORY.md disappeared from the panel because state was a single nullable
   payload. **Status: closed.** `MemoryPanel` props refactored from
   `event: MemoryWritePayload | null` to `entries: Record<string, MemoryWritePayload>`.
   Render iterates entries sorted alphabetically by `target` (matching server-side sort
   in `_collect_memory_status`). New `seedFromStatusEntry` adapter promotes wire status
   entries into the panel's payload shape; `statusTag` returns `"idle"` for entries seeded
   without an associated action. Per-target updates preserve the other file's visibility.

Skipped from this round: `display.sections.memory` config + `/details memory ...` runtime
toggle (Hermes-style per-section visibility). Pure convention follow with no functional
gap; not blocking. Add when a second OC-specific panel needs the same machinery.
