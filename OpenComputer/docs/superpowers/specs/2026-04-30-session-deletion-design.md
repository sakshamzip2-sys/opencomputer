# Session Deletion — Design

**Date:** 2026-04-30
**Status:** Draft → audit → plan → execute (planned 3-PR slice)
**Author:** Saksham + Claude
**Spec lives at:** `docs/superpowers/specs/2026-04-30-session-deletion-design.md`

---

## 1. Problem

The `oc resume` picker shows a flat list of every session ever created in the current profile. The screenshot of a real user state shows **36 sessions in 3 days**, of which the majority are `(untitled · <hash>)` rows with single-digit message counts — the residue of accidental TUI launches, `/exit`-immediately experiments, and 1-message "test" pings. There is currently no way to delete a session:

- `SessionDB` has no `delete_session()` method (verified at [opencomputer/agent/state.py](../../opencomputer/agent/state.py) — only `create_session`, `get_session`, `list_sessions`, `set_session_title`, `get_session_vibe`, etc.).
- The `oc session` CLI ([opencomputer/cli_session.py](../../opencomputer/cli_session.py)) has `list / show / fork / resume`, no `delete` or `prune`.
- The full-screen picker [opencomputer/cli_ui/resume_picker.py](../../opencomputer/cli_ui/resume_picker.py) handles `↑↓ enter esc Ctrl+C` only — no delete keybinding.

Users have no in-app deletion path. The only workaround is `sqlite3 ~/.opencomputer/<profile>/sessions.db` and hand-deletion — completely off-piste from a personal-agent UX standpoint.

## 2. What Claude Code does (reference)

I checked the locally cloned reference at `sources/claude-code/`. Two relevant patterns:

1. **Terminal TUI: no in-picker delete.** The Claude Code `/resume` picker mirrors ours — keyboard navigation only, no `d` or delete binding. Users `rm` JSONL session files in `~/.claude/projects/` manually.
2. **VSCode extension: delete button.** Claude Code's VSCode extension has a per-row delete UI (CHANGELOG line 920: "Fixed delete button not working for Untitled sessions"). The TUI does not.
3. **Auto-prune via `cleanupPeriodDays`** (CHANGELOG line 2952 introduction; line 225 confirms 30-day default; line 497 says `0` is rejected). At startup, sessions older than the threshold are deleted in the background. This is Claude Code's load-bearing "clutter" answer — they don't lean on a delete button, they lean on TTL.

**Implication:** Claude Code does not give terminal TUI users a delete affordance. We can do better than the reference here — a cheap `d` keybinding in our picker is a strict improvement.

## 3. Scope (this spec)

In scope:

- **PR-1**: SessionDB.delete_session() + `oc session delete <id>` CLI + `d` keybinding in resume picker.
- **PR-2**: `oc session prune` with `--untitled / --older-than / --empty / --dry-run / --yes` filters.
- **PR-3**: Optional config knob `session.auto_prune_days` (and friends) that runs prune at startup.

Out of scope:

- Soft-delete / trash bin (added complexity, no signal demand).
- "Restore from trash" undo flow.
- Cross-profile bulk operations (each profile has its own `sessions.db`; out of scope).
- Cleaning up `audit_log` rows (audit is append-only and tamper-evident by F1 contract — must not be touched).
- Cleaning up Honcho overlay memory (separate datastore; lifecycle tied to user, not to a single session).

## 4. Architecture

### 4.1 Storage layer — `SessionDB.delete_session(id)`

Single new method on [SessionDB](../../opencomputer/agent/state.py):

```python
def delete_session(self, session_id: str) -> bool:
    """Delete a session and all rows that cascade from it.

    Returns True if a row was removed, False if no session had that id.

    Cascades (via FOREIGN KEY ... ON DELETE CASCADE):
        - messages → also clears messages_fts via DELETE trigger
        - episodic_events → also clears episodic_fts via DELETE trigger
        - vibe_log (no cascade FK; manual DELETE).
        - tool_usage (no cascade FK; manual DELETE).

    Does NOT touch:
        - audit_log (F1 immutable audit log; kept for compliance).
        - consent_grants / consent_counters (per-capability, not per-session).
    """
```

Implementation notes:

- Uses the same `with self._connect() as conn:` context manager. `PRAGMA foreign_keys=ON` is already set in `_connect` at [state.py:448](../../opencomputer/agent/state.py#L448), so cascade fires correctly.
- For `vibe_log` and `tool_usage` (which lack cascade FKs in the v5/v6 schema additions), explicit `DELETE FROM <table> WHERE session_id = ?` runs in the same transaction.
- Returns `bool` from `cursor.rowcount > 0`. Allows callers to print "session not found" without an extra `SELECT`.
- Writes one audit-log row per delete (capability `session.delete`, scope `session_id`) for traceability — F1 contract compatible.

### 4.2 CLI — `oc session delete <id>`

New subcommand in [opencomputer/cli_session.py](../../opencomputer/cli_session.py):

```python
@session_app.command("delete")
def session_delete(
    session_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a session and all its messages."""
```

Behavior:

- Refuses to delete the *currently active* session id when called inside a running `oc chat` (we won't enforce this in PR-1 because the CLI subcommand is a separate process — the OS-level guard is "you can't run `oc session delete X` from inside the same TUI that's writing to X". Add a stdin-tty check + warning if `OPENCOMPUTER_SESSION_ID` is in the env.)
- Truncates `id` to 8-char prefix in the confirmation prompt for readability.
- `--yes` skips the prompt (scriptable).
- Exit codes: `0` success, `1` not found, `130` user-cancelled at confirm.

### 4.3 TUI — `d` keybinding in resume picker

Mutate [opencomputer/cli_ui/resume_picker.py](../../opencomputer/cli_ui/resume_picker.py) to add:

- A `d` keybinding when the focus is in the search buffer with empty query (so we don't shadow typing the letter `d` as part of a filter). Preferred fallback: bind to `Keys.ControlD` so it always works regardless of buffer focus.
- An inline confirm row: when `d` is pressed, the row's "title" line is replaced with `delete this session? [y / N]` and the keybindings narrow to `y / n / esc`. No full-screen overlay (would crowd the layout).
- On `y`: call `db.delete_session(row.id)` → mutate `rows` and `state["filtered"]` in place → re-render → keep the cursor position (clamp if it overshoots the last row).
- On `n` / `esc` / `Ctrl+C` during confirm: return to navigation mode without deleting.
- Footer updates: `↑↓ navigate    enter resume    d delete    esc cancel`.

Style additions: a `class:row.confirm.delete` hot-red foreground for the inline prompt, plus a `class:row.title.confirming` muted-yellow style for visual feedback.

This is implemented with a small state machine inside the closure — `state["mode"]` flips between `"navigate"` and `"confirm-delete"`. Refactoring isn't needed; the existing closure pattern handles a 2-state extension cleanly.

### 4.4 Auto-prune — config schema

New section in `config.yaml`:

```yaml
session:
  auto_prune_days: 0          # 0 = disabled (default). >0 = prune at startup.
  auto_prune_untitled_days: 7 # Untitled sessions get a tighter TTL (separate from above).
  auto_prune_min_messages: 3  # Sessions with fewer than this AND age >= untitled TTL are pruned.
```

Behavior:

- All three default to values that **do not delete anything** (auto_prune_days=0 means disabled). Opt-in.
- Prune runs once at agent startup (`AgentLoop.__init__` → calls `SessionDB.auto_prune(...)`), inside the same tx as schema migration. Bounded — at most 200 rows deleted per run to avoid startup latency surprises (tunable internally; not a config knob).
- Audit row written per delete with capability `session.auto_prune`.
- Logs the count to stderr at startup (`auto-pruned 12 stale sessions (older than 90 days, untitled+empty)`).

### 4.5 `oc session prune` CLI

New subcommand:

```python
@session_app.command("prune")
def session_prune(
    older_than: str | None = typer.Option(None, "--older-than", help="e.g. 30d, 6w, 3mo"),
    untitled: bool = typer.Option(False, "--untitled"),
    empty: bool = typer.Option(False, "--empty",
        help="Sessions with message_count <= 1 (initial system message)."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
```

Filter composition: AND. If no filters, refuse (don't accidentally nuke everything).

`--dry-run` prints the table that *would* be deleted, no changes. Default mode prints the table, asks "delete N sessions? [y/N]", proceeds on yes.

`--older-than` parsing:
- Suffix-based: `30d` (days), `6w` (weeks), `3mo` (months ≈ 30d), `1y` (years ≈ 365d).
- No suffix → reject with error (unambiguous).
- Negative or zero → reject.

## 5. Data flow

```
oc resume                          oc session delete <id>
     │                                       │
     ▼                                       │
run_resume_picker                            │
     │  ↑↓                                   │
     │  d  → confirm overlay                 │
     │  y  ────────────────────► db.delete_session(id) ◄────────
     │     ↓                            │
     │  re-render rows                  │ writes audit_log row
     │                                  │ cascade: messages, FTS,
     │                                  │ episodic_events, vibe_log,
     │                                  │ tool_usage
     ▼                                  │
selected session id ── or None ─        ▼
                                  return bool deleted
```

Auto-prune runs at AgentLoop startup before the user enters the loop:

```
oc chat / oc code / oc resume
     │
     ▼
AgentLoop.__init__
     │ if config.session.auto_prune_days > 0:
     ▼
db.auto_prune(...)
     │ SELECT id FROM sessions WHERE
     │   started_at < (now - auto_prune_days * 86400)
     │   OR (started_at < (now - auto_prune_untitled_days * 86400)
     │       AND title = '' AND message_count < auto_prune_min_messages)
     │ LIMIT 200
     ▼
for each id: db.delete_session(id), audit row
log to stderr: "auto-pruned N sessions"
```

## 6. Tests

PR-1:
- `tests/test_session_delete.py`:
  - `test_delete_existing_session_cascades_messages_and_fts()`
  - `test_delete_returns_false_for_unknown_id()`
  - `test_delete_writes_audit_row()`
  - `test_delete_does_not_touch_other_sessions()`
- `tests/test_cli_session_delete.py`:
  - `test_delete_command_with_yes_flag()`
  - `test_delete_command_prompts_then_aborts()`
  - `test_delete_unknown_session_exits_1()`
- `tests/cli_ui/test_resume_picker_delete.py`:
  - `test_d_key_enters_confirm_mode()`
  - `test_y_during_confirm_calls_delete_and_rerenders()`
  - `test_n_during_confirm_returns_to_navigation()`
  - `test_delete_clamps_selected_idx_when_last_row_removed()`

PR-2:
- `tests/test_cli_session_prune.py`:
  - `test_prune_requires_at_least_one_filter()`
  - `test_prune_dry_run_makes_no_changes()`
  - `test_prune_older_than_30d_picks_correct_rows()`
  - `test_prune_untitled_filter_skips_named_sessions()`
  - `test_prune_empty_filter_picks_message_count_le_1()`
  - `test_prune_filters_combine_with_AND()`
  - `test_older_than_invalid_format_exits_1()`

PR-3:
- `tests/test_auto_prune_at_startup.py`:
  - `test_auto_prune_disabled_when_days_is_zero()`
  - `test_auto_prune_runs_when_configured()`
  - `test_auto_prune_caps_at_200_rows_per_run()`
  - `test_auto_prune_writes_one_audit_row_per_delete()`

## 7. Risks

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | `PRAGMA foreign_keys=ON` not set, cascade silently no-ops | HIGH | **Verified set** at [state.py:448](../../opencomputer/agent/state.py#L448). Add a regression assertion. |
| 2 | User accidentally deletes important session via `d` key | MEDIUM | Two-keystroke confirm (`d` then `y`); `n` is the default if user hits any other key. |
| 3 | `oc session delete <id>` deletes the session a parallel `oc chat` is writing to | MEDIUM | Read-modify-write race on SQLite WAL. Document that delete is best run when no live chat exists. Add an env-var check (warn if `OPENCOMPUTER_SESSION_ID` matches). |
| 4 | Auto-prune nukes a session the user wanted | MEDIUM | Default OFF; opt-in only. Cap at 200 rows/run. Log count to stderr. |
| 5 | `--older-than` parser misreads `30` as 30 seconds | LOW | Reject suffix-less input with a clear error message. |
| 6 | Audit log fills up with `session.auto_prune` rows | LOW | Audit log is the user's record; that's the intended cost. F1 already accounts for this volume. |
| 7 | Picker state-machine confusion if user spam-presses `d` | LOW | While in confirm-mode, only `y/n/esc/Ctrl+C` are bound; `d` becomes a no-op. |
| 8 | `tool_usage` and `vibe_log` lack cascade FK; rows leak | LOW | Explicit DELETE in the same tx as the session row; verified by test. |
| 9 | F1 audit_log cleanup is illegal | HIGH | Spec is explicit: never touched. Test asserts audit_log row count grows, not shrinks. |

## 8. Migration

No schema migration needed. `SessionDB` schema v6 already has every needed FK and trigger; we only add a method and CLI surface.

Config schema gets new `session.*` keys with safe defaults (auto_prune_days=0). Existing configs without these keys behave exactly as before.

## 9. Out of scope (deferrals)

- **Soft-delete / trash.** Adds a state machine, requires UI for restoring. Defer until a real "I deleted that by accident" complaint surfaces.
- **Cross-profile prune.** Each profile has its own `sessions.db`; orchestrating across them is a separate concern (`oc profile each ... session prune`).
- **Honcho memory cleanup.** That's a Honcho-side question — sessions and Honcho memory have different lifecycles by design.
- **Telemetry on prunes.** No telemetry pipeline exists today; not adding one for this feature.

---

## 10. Open questions for review

- (Resolved) Auto-prune default: 90 days, OFF by default — opt-in via config.
- (Resolved) Confirm UX: inline 2-key (`d` then `y`), no full-screen overlay.
- (Resolved) Audit-log policy: write per-delete; never touch existing audit rows.
- (Resolved) Per-profile config: yes — `sessions.db` is per-profile, so `session.*` keys live in the per-profile `config.yaml`.

