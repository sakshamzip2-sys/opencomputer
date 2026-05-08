# Worktree-Include + Checkpoint Hygiene — Production-Grade Design

**Date:** 2026-05-08
**Author:** seedicon session
**Branch (target):** `feat/worktree-checkpoint-hygiene-2026-05-08`
**Coordination:** parallel `dev` session is on `feat/gateway-parity-pr1-2026-05-08` (PR #488, "Task B7" + later). This spec stays out of all gateway/channels/dispatch/messaging code.

---

## Problem statement

Two correctness/hygiene gaps surfaced when comparing the Hermes documentation pasted by the user against the OpenComputer (OC) codebase:

1. **`oc code -w` is broken for any project that uses gitignored runtime files.** Today `opencomputer/worktree.py` calls `git worktree add` and chdirs in. The fresh worktree has no `.env`, no `.venv/`, no `node_modules/`, no `.opencomputer/` profile state — meaning the agent that lands inside cannot run tests, hit external APIs, or use installed dependencies. Hermes ships `.worktreeinclude` to fix exactly this. OC has the worktree machinery but not the include layer.
2. **`RewindStore` (the on-disk checkpoint store backing `/rollback`, `/undo`, `/checkpoint`, and the `auto_checkpoint` PreToolUse hook) has no GC, no size cap, no retention policy, and no user-facing CLI.** Each session creates `~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/{meta.json, files/}` and these accumulate forever. Sessions die but their checkpoint dirs do not. Active coding-harness users will eventually have multi-GB stores.

Both are operational bugs in shipped features, not new feature requests.

---

## Non-goals

- Replacing `RewindStore`'s on-disk format with a shadow git repo. The current design works; switching is YAGNI.
- Adding per-task auxiliary model overrides (vision/title/compression/...). OC's existing `aux_llm.py` + `auxiliary_client.py` are sufficient for the single-user case.
- Hardline-blocklist tier, `--yolo` mode, website blocklist, MCP env-strip — solve multi-tenant problems OC does not have.
- Touching anything in `channels/`, `dispatch/`, `cli_gateway*`, gateway adapters, or messaging plumbing — owned by `dev` session.
- Symlink-based include resolution. Symlinked `.env` edited in the worktree would write back to the main repo. Reject.
- Concurrent live mutation while prune runs is bounded but not perfectly serializable; we use file-locks + atomic per-dir delete to avoid corruption, accepting that a save mid-prune may briefly count both ways.

---

## Architecture

### Module map

```
opencomputer/
  worktree.py                    [MODIFIED] — call into worktree_include after add
  worktree_include.py            [NEW]      — parse + expand + copy logic
  cli_worktrees.py               [NEW]      — `oc worktrees list/clean/include-preview` Typer subapp
  cli_checkpoints.py             [NEW]      — `oc checkpoints status/prune/clear` Typer subapp
  checkpoint_admin.py            [NEW]      — cross-session enumeration + aggregate ops
  agent/config.py                [MODIFIED] — add `worktree.*` and `checkpoints.*` sections
  cli.py                         [MODIFIED] — add_typer for worktrees + checkpoints subapps

extensions/coding-harness/
  rewind/store.py                [MODIFIED] — total_size_bytes, count, oldest, newest,
                                              prune, clear, should_auto_prune, mark_pruned;
                                              save() takes optional max_total_bytes
  rewind/checkpoint.py           [MODIFIED] — Checkpoint gains `excluded_files` field;
                                              from_files() takes max_file_size_bytes
  rewind/__init__.py             [MODIFIED] — export PruneReport
  hooks/auto_checkpoint.py       [MODIFIED] — auto-prune-on-startup logic

tests/
  test_worktree_include.py                        [NEW]
  test_cli_worktrees.py                           [NEW]
  test_rewind_store_prune.py                      [NEW]
  test_checkpoint_admin.py                        [NEW]
  test_cli_checkpoints.py                         [NEW]
  test_auto_checkpoint_prune.py                   [NEW]

OpenComputer/docs/cli/
  checkpoints.md                                  [NEW]
  worktrees.md                                    [NEW]
```

### Section A — `.worktreeinclude` (Gap A)

#### A.1 Parse

`opencomputer/worktree_include.py::parse_worktreeinclude(path: Path) -> list[str]`

- Gitignore-style: each line stripped; lines starting with `#` are comments; blank lines ignored; trailing whitespace trimmed.
- Missing file → returns `[]` (silent — `.worktreeinclude` is opt-in).
- Patterns are *strings* at this layer; expansion happens in `expand_patterns`.

#### A.2 Expand

`opencomputer/worktree_include.py::expand_patterns(repo_root: Path, patterns: list[str]) -> list[Path]`

- For each pattern, glob via `repo_root.glob(pattern)`.
- A pattern that is a literal path (e.g. `.env`) resolves to `[repo_root / ".env"]` if it exists.
- A pattern that is a directory (`.venv/` or `.venv`) resolves to the dir itself; copy is recursive.
- Glob patterns (`config/*.local.yaml`) expand to all matches.
- Dedupe: a `Path` is included once even if multiple patterns reach it.
- Patterns reaching outside `repo_root` (`..`) are rejected with a warning + skip.
- Returns paths in deterministic sorted order for log stability.

#### A.3 Copy

`opencomputer/worktree_include.py::copy_into_worktree(sources: list[Path], repo_root: Path, worktree: Path, *, dry_run: bool = False, max_total_mb: int = 1000, max_per_file_mb: int = 500, follow_symlinks: bool = False) -> CopyReport`

Algorithm:

1. Compute total bytes (recursive for dirs); if > `max_total_mb * 1024**2`, raise `WorktreeIncludeTooLargeError(total, cap, oversize_paths)`. The caller surfaces a clear error.
2. For each source path, compute `dst = worktree / src.relative_to(repo_root)`.
3. If `dry_run`: record entry in `CopyReport.copied` (with bytes), no I/O.
4. Otherwise:
   - File: copy to `dst.with_suffix(dst.suffix + ".tmp.<rand>")`, then `os.replace` to final → atomic.
   - Directory: walk recursively, replicate hierarchy, atomic per-file rename.
   - Symlink: if `follow_symlinks=False`, copy the link target (not the dereferenced contents) via `os.symlink`. Detect cycles via realpath set; on cycle, log warning, skip.
   - Permission denied: log + record in `failed`, continue with next source.
   - File > `max_per_file_mb`: log warning + record in `skipped`, continue.
5. Use `shutil.copy2` for files (preserves mode + mtime).
6. Return `CopyReport`.

#### A.4 Resolution order

1. `<repo_root>/.worktreeinclude` (project-specific) — required to be UTF-8.
2. `<OPENCOMPUTER_HOME>/worktreeinclude` (global fallback — same syntax) — only consulted if `worktree.include_global_fallback` is true (default).

Patterns from both are unioned. Project takes precedence on duplicates.

#### A.5 Wire-in

`opencomputer/worktree.py::session_worktree(...)` updated:

```python
@contextmanager
def session_worktree(
    cwd: Path,
    *,
    session_id: str | None = None,
    branch: str | None = None,
    keep: bool = False,
    include_dry_run: bool = False,
) -> Iterator[Path]:
    original_cwd = Path.cwd()
    wt = create_session_worktree(cwd, session_id=session_id, branch=branch)
    if wt is None:
        yield original_cwd
        return

    # NEW: copy .worktreeinclude content into wt before chdir
    rr = repo_root(cwd)
    if rr is not None:  # always non-None here since create_session_worktree succeeded
        try:
            _maybe_apply_worktreeinclude(rr, wt, dry_run=include_dry_run)
        except WorktreeIncludeTooLargeError:
            # Fail loudly — silent half-populated worktree is worse than a hard error.
            remove_session_worktree(wt)
            raise

    os.chdir(wt)
    try:
        yield wt
    finally:
        os.chdir(original_cwd)
        if not keep:
            remove_session_worktree(wt)
```

`_maybe_apply_worktreeinclude(repo_root, worktree, *, dry_run)` reads the project file (`<repo_root>/.worktreeinclude`) AND, if `worktree.include_global_fallback` is true, the global file (`<profile_home()>/worktreeinclude` — no leading dot, since it lives in the OC home itself). Patterns are unioned with project-precedence on duplicates. The function calls `expand_patterns` then `copy_into_worktree` and logs an INFO summary on success.

#### A.6 CLI surface

In a new `opencomputer/cli_worktrees.py`:

- `oc worktrees list` — print all `.opencomputer-worktrees/*` for cwd's repo, with branch + age.
- `oc worktrees clean [--dry-run] [--all]` — remove stale worktrees whose branch is gone (or all `.opencomputer-worktrees/*` if `--all`).
- `oc worktrees include-preview [--dir PATH]` — read project + global `.worktreeinclude`, expand, print would-copy report; no I/O.

Plumb via `cli.py`: `app.add_typer(worktrees_app, name="worktrees")`.

#### A.7 Config

In `agent/config.py` (new section, default values shown):

```yaml
worktree:
  include_max_total_mb: 1000
  include_max_per_file_mb: 500
  include_global_fallback: true
  include_follow_symlinks: false
```

#### A.8 Edge cases

| Case | Behavior |
|---|---|
| `.worktreeinclude` missing | no-op, debug log |
| Pattern matches nothing | debug log "no matches for X" |
| Pattern escapes `repo_root` (`../foo`) | warn + skip pattern |
| Total above `include_max_total_mb` | abort with `WorktreeIncludeTooLargeError`; worktree removed |
| Single file above `include_max_per_file_mb` | warn + skip file; other files still copied |
| Symlink cycle | warn + skip path; other files still copied |
| Permission denied on source | log failure + continue |
| Worktree dir read-only | hard error (this is real broken state) |
| Concurrent agent writes during copy | atomic temp+rename per file makes individual files safe; ordering across files is best-effort |
| Stale worktree from prior failed session | `oc worktrees clean` removes |

---

### Section B — Checkpoint hygiene (Gap B)

#### B.1 Existing layout (verified)

```
~/.opencomputer/harness/
  <session_id>/
    rewind/
      [<subagent_id>/]            ← optional sub-store per subagent
        <checkpoint_id>/
          meta.json
          files/
            path-with-slashes-replaced-by-double-underscore
```

`HARNESS_ROOT = Path.home() / ".opencomputer" / "harness"` — see `extensions/coding-harness/plugin.py:67`.

The store is **per-session**, not per-workspace. Subagent dirs are nested children of a session's rewind dir.

#### B.2 `RewindStore` enhancements

`extensions/coding-harness/rewind/store.py`:

```python
class RewindStore:
    # existing __init__/save/save_shielded/load/list/restore unchanged

    def total_size_bytes(self, *, include_subagents: bool = True) -> int: ...
    def count(self, *, include_subagents: bool = True) -> int: ...
    def oldest(self) -> Checkpoint | None: ...
    def newest(self) -> Checkpoint | None: ...

    def prune(
        self,
        *,
        older_than_days: int | None = None,
        max_total_bytes: int | None = None,
        max_count: int | None = None,
        delete_orphans: bool = True,
        dry_run: bool = False,
    ) -> PruneReport: ...

    def clear(self) -> int: ...

    def should_auto_prune(self, *, min_interval_hours: int = 24) -> bool: ...
    def mark_pruned(self) -> None: ...

    def save(
        self,
        cp: Checkpoint,
        *,
        max_total_bytes: int | None = None,
    ) -> None: ...
```

Prune algorithm:

```
1. Walk store dirs (recursive, include subagents).
2. Identify orphans: dirs whose meta.json is missing OR malformed.
3. If delete_orphans: schedule them for deletion.
4. Build (id, created_at, bytes) tuples for valid checkpoints.
5. If older_than_days: schedule any whose created_at < (now - older_than_days).
6. If max_count and count > cap: schedule oldest (by created_at) for deletion until count == cap.
7. If max_total_bytes and total > cap: schedule oldest for deletion until total <= cap.
8. If dry_run: build PruneReport with would-delete list and return without I/O.
9. Otherwise: execute deletions (rename to .pending_delete dir, then rmtree); return PruneReport.
```

Atomicity: each scheduled dir is renamed to a sibling `.pending_delete/<id>` first, so a crash mid-prune leaves a clean recoverable state on next run.

`save()` with `max_total_bytes` applies pre-write eviction: drops oldest valid checkpoint(s) until `total + new < cap`. Logs each eviction at INFO.

#### B.3 `Checkpoint` enhancement

`extensions/coding-harness/rewind/checkpoint.py`:

```python
@dataclass(frozen=True)
class Checkpoint:
    id: str
    files: Mapping[str, bytes]
    label: str
    created_at: str
    excluded_files: tuple[str, ...] = ()    # NEW

    @staticmethod
    def from_files(
        files: Mapping[str, bytes],
        *,
        label: str,
        max_file_size_bytes: int | None = None,    # NEW
    ) -> Checkpoint:
        ...
```

When `max_file_size_bytes` is set, files exceeding it are excluded from `files` and recorded in `excluded_files`. The hash digest is computed only over included files. `meta.json` serializes `excluded_files`.

Backwards-compat: existing checkpoints without `excluded_files` in `meta.json` load with `excluded_files=()`.

#### B.4 `PruneReport`

```python
@dataclass(frozen=True)
class PruneReport:
    dropped: tuple[str, ...]
    kept: int
    orphans_removed: tuple[str, ...]
    bytes_freed: int
    bytes_remaining: int
    dry_run: bool
```

#### B.5 Cross-session admin

`opencomputer/checkpoint_admin.py`:

```python
@dataclass(frozen=True, slots=True)
class PrunePolicy:
    """Policy bundle passed to RewindStore.prune and prune_all."""
    older_than_days: int | None = None
    max_total_bytes: int | None = None
    max_count: int | None = None
    delete_orphans: bool = True
    dry_run: bool = False

    @classmethod
    def from_config(cls, cfg: CheckpointsConfig) -> "PrunePolicy": ...

@dataclass(frozen=True, slots=True)
class StoreInfo:
    session_id: str
    path: Path
    count: int                  # includes subagent checkpoints
    size_bytes: int             # includes subagent dirs
    oldest_iso: str | None
    newest_iso: str | None
    last_prune_iso: str | None
    subagent_count: int         # 0 if no subagent dirs

@dataclass(frozen=True, slots=True)
class AggregateReport:
    stores: tuple[StoreInfo, ...]
    total_size_bytes: int
    total_count: int

def harness_root() -> Path: ...
def iter_stores() -> Iterator[StoreInfo]: ...
def aggregate_status() -> AggregateReport: ...
def prune_all(*, policy: PrunePolicy, session_filter: str | None = None) -> dict[str, PruneReport]: ...
def clear_all(*, session_filter: str | None = None) -> int: ...
```

- `harness_root()` returns `profile_home() / "harness"` (defaults to `Path.home() / ".opencomputer" / "harness"` for the active profile; respects `OPENCOMPUTER_HOME` per the existing profiles module).
- `iter_stores()` enumerates direct children of `harness_root()` whose `<sid>/rewind/` directory exists. Each yielded `StoreInfo` has subagent dirs FLATTENED into its `count` and `size_bytes` (one `StoreInfo` per session, never one per subagent).
- `subagent_count` is exposed for the `oc checkpoints status` row so users can see which sessions have multi-subagent activity.

#### B.6 CLI

`opencomputer/cli_checkpoints.py` — new Typer subapp wired via `cli.py::app.add_typer(checkpoints_app, name="checkpoints")`:

- `oc checkpoints status` — Rich table; per-session row + global totals; auto-prune state.
- `oc checkpoints prune [OPTIONS]` — flags map onto `PrunePolicy(older_than_days, max_total_bytes_mb, max_count, delete_orphans, dry_run)`. Default policy reads config.
- `oc checkpoints clear [--session SID] [--yes]` — destructive; refuses without `--yes` unless stdin is a TTY and user confirms.

Each subcommand uses Rich Console for output and exits with code 0 on success, 2 on user-recoverable error, 1 on hard failure.

#### B.7 Auto-prune-on-startup

`extensions/coding-harness/hooks/auto_checkpoint.py` — augment the existing handler:

```python
async def handler(ctx: HookContext) -> HookDecision | None:
    ...
    if config.checkpoints.auto_prune and store.should_auto_prune(
        min_interval_hours=config.checkpoints.min_interval_hours
    ):
        store.mark_pruned()  # eager mark so concurrent fires don't race
        asyncio.create_task(_background_prune(store, policy))
    ...
```

`_background_prune` swallows + logs exceptions (WARNING) — never blocks the save path.

`should_auto_prune` reads `<store.root>/.last_prune` mtime; missing means True. `mark_pruned` writes the file atomically.

#### B.8 Config

In `agent/config.py`:

```yaml
checkpoints:
  enabled: true
  max_snapshots: 50          # per session
  max_total_size_mb: 1000    # global cap, enforced cross-session
  max_file_size_mb: 50       # skip files larger than this
  auto_prune: true
  retention_days: 30
  min_interval_hours: 24
  delete_orphans: true
```

#### B.9 Edge cases

| Case | Behavior |
|---|---|
| corrupt meta.json | orphan; removed by `--delete-orphans` (default true) |
| concurrent prune+save | file-lock on `.last_prune`; if locked, skip auto-prune (CLI prune always runs) |
| symlink store path | `os.path.realpath` before counting |
| empty store | status prints `(no checkpoints yet)` |
| stale session (not in sessions DB) | orphan candidate; user opts in via `--delete-orphans` |
| crash mid-prune | `.pending_delete/` dir is recovered (or rmtree-cleaned) on next prune |
| huge prune (>10K dirs) | batched delete with INFO progress every 1000 |
| CLI `clear` with TTY-less stdin and no `--yes` | refuse (exit 2) — never silent destruction |
| dry-run | no I/O; only reports |
| auto-prune failure | log WARNING; save still proceeds |

---

### Section C — Cross-cutting

- **Profile-scoped paths:** all consumers use `opencomputer.profiles.profile_home()` rather than hardcoding `Path.home() / ".opencomputer"`. `harness_root()` does the same.
- **Atomic ops everywhere.** Worktreeinclude copy: `.tmp.<rand>` + `os.replace`. Prune: rename to `.pending_delete/`, then `rmtree`. `mark_pruned`: atomic `os.replace` swap.
- **Logging.** All paths log under named loggers:
  - `opencomputer.worktree.include`
  - `opencomputer.cli.worktrees`
  - `opencomputer.cli.checkpoints`
  - `coding_harness.rewind.store`
  - `coding_harness.rewind.prune`
- **Backwards-compat.** All new method params are optional kwargs. `meta.json` schema additions tolerate absence.
- **Coordination with `dev`:** zero edits in `channels/`, `dispatch/`, `cli_gateway*`, `gateway*`, any messaging adapter, `cli_pair.py`, or anything in dev's worktree's diff. `git diff main..feat/gateway-parity-pr1-2026-05-08` is the boundary; we touch nothing in that diff.
- **Documentation.**
  - Per-CLI: `OpenComputer/docs/cli/checkpoints.md` and `worktrees.md` (new).
  - SKILL.md: `extensions/coding-harness/skills/.../SKILL.md` updated to mention prune + auto-prune.
  - Module docstrings on every new module.
  - Typer help strings on every command + flag.

---

## Tests

### A — worktree_include (~18 cases)

`tests/test_worktree_include.py`:

- `test_parse_basic` — comments, blanks, normal lines.
- `test_parse_missing_file` — empty list.
- `test_parse_invalid_utf8` — UnicodeDecodeError handled gracefully.
- `test_expand_literal_file` — `.env` resolves to repo_root/.env if exists.
- `test_expand_directory` — `.venv/` resolves to dir.
- `test_expand_glob` — `config/*.yaml` resolves.
- `test_expand_no_match_pattern` — empty result + debug log.
- `test_expand_escape_repo_root_rejected` — `../bar` skipped with warning.
- `test_expand_dedupe` — same target via two patterns appears once.
- `test_copy_file_preserves_mode_mtime`.
- `test_copy_directory_recursive`.
- `test_copy_symlink_no_follow` — link copied as link.
- `test_copy_recursive_symlink_cycle_skipped`.
- `test_copy_permission_denied_continues`.
- `test_copy_size_cap_aborts` — `WorktreeIncludeTooLargeError`.
- `test_copy_per_file_size_skips`.
- `test_copy_atomic_temp_rename` — temp file does not exist after success.
- `test_copy_dry_run_no_io` — disk unchanged.
- `test_session_worktree_applies_include` — full integration, real `git init` + worktree add.
- `test_session_worktree_global_fallback` — global file used when project missing.

### B — RewindStore prune (~22 cases)

`tests/test_rewind_store_prune.py`:

- `test_total_size_bytes_empty`, `test_total_size_bytes_populated`, `test_total_size_bytes_includes_subagents`.
- `test_count_empty`, `test_count_populated`, `test_count_excludes_subagents_dir_not_subagent_checkpoints`.
- `test_oldest_newest_with_data`, `test_oldest_newest_empty_returns_none`.
- `test_prune_no_policy_is_orphans_only`.
- `test_prune_older_than_days`.
- `test_prune_max_count_drops_oldest`.
- `test_prune_max_total_bytes_drops_oldest_until_under_cap`.
- `test_prune_orphan_corrupt_meta_removed`.
- `test_prune_dry_run_no_filesystem_changes`.
- `test_prune_pending_delete_recovers_after_crash` — simulate `.pending_delete/` left over → next prune cleans it.
- `test_save_evicts_oldest_when_exceeding_cap`.
- `test_save_max_total_bytes_one_evicted`.
- `test_should_auto_prune_first_call_true`.
- `test_should_auto_prune_within_window_false`.
- `test_should_auto_prune_after_window_true`.
- `test_mark_pruned_writes_atomic_file`.
- `test_clear_returns_count_and_wipes`.
- `test_clear_preserves_last_prune_marker`.
- `test_concurrent_save_during_prune` — two threads; no corruption.
- `test_checkpoint_excludes_large_files` — `Checkpoint.from_files(max_file_size_bytes=N)`.

### C — checkpoint_admin (~12 cases)

`tests/test_checkpoint_admin.py`:

- `test_iter_stores_empty`.
- `test_iter_stores_multiple_sessions`.
- `test_iter_stores_skips_non_dirs`.
- `test_iter_stores_skips_non_rewind_dirs`.
- `test_aggregate_status_totals`.
- `test_aggregate_status_per_session_breakdown`.
- `test_prune_all_applies_policy_per_store`.
- `test_prune_all_session_filter`.
- `test_clear_all_yes`.
- `test_clear_all_session_filter`.
- `test_harness_root_respects_OPENCOMPUTER_HOME_env`.
- `test_iter_stores_handles_unreadable_dir` (graceful skip).

### D — CLI ~ 12 cases (`oc worktrees` ~5, `oc checkpoints` ~7)

`tests/test_cli_worktrees.py`:

- `test_worktrees_list_empty`.
- `test_worktrees_list_populated`.
- `test_worktrees_clean_dry_run`.
- `test_worktrees_clean_removes_orphans`.
- `test_worktrees_include_preview_dry_run_format`.

`tests/test_cli_checkpoints.py`:

- `test_status_empty`.
- `test_status_populated`.
- `test_prune_default_policy_from_config`.
- `test_prune_flags_override_config`.
- `test_prune_dry_run_outputs_report`.
- `test_clear_yes_wipes_all`.
- `test_clear_without_yes_no_tty_aborts`.

### E — auto_checkpoint hook (~6 cases)

`tests/test_auto_checkpoint_prune.py`:

- `test_first_fire_triggers_prune`.
- `test_within_min_interval_skips_prune`.
- `test_after_min_interval_triggers_prune`.
- `test_prune_failure_does_not_block_save`.
- `test_concurrent_fires_do_not_double_prune`.
- `test_disabled_in_config_skips_prune_path`.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Worktreeinclude copies leak secrets to a worktree path that's then committed | The worktree is on a fresh branch; if the user commits secrets, that's on them, not us. We document this explicitly in `docs/cli/worktrees.md`. |
| Big copy slows `oc code -w` startup | Log progress; cap at `include_max_total_mb`; user can lower the cap. Acceptable tradeoff: slow startup is preferable to the agent erroring on its first test run. |
| Auto-prune deletes a checkpoint the user wanted | Defaults are conservative (50 snapshots, 30 days, 1 GB). User-disable: `checkpoints.auto_prune: false`. |
| `dev` session lands gateway changes that conflict with this branch | Zero file overlap is checked at PR-creation time; spec restricts to non-gateway paths. CI will catch any unexpected overlap. |
| `.last_prune` clock skew in containers | We use `os.path.getmtime`; clock skew within ±5 minutes tolerable. Accepted. |
| Rapid back-to-back prunes (e.g. CI loop) | `min_interval_hours` enforces idempotency by design. |
| Hardcoded `.opencomputer-worktrees` collides with future `.git/worktrees` semantics | Path namespaced under repo root, not under `.git/`; safe. |

---

## Acceptance criteria

A reviewer should be able to verify production-grade by running:

1. `oc worktrees include-preview` in a Python project with `.env` and `.venv/` — sees both queued for copy with byte counts.
2. `oc code -w` in same project — agent lands inside worktree, can `python -c "import x"` against `.venv/`, can read `.env`.
3. `oc checkpoints status` — Rich table of all sessions' stores + totals + auto-prune state.
4. `oc checkpoints prune --older-than 7 --dry-run` — produces report without deletion.
5. `oc checkpoints prune --max-size 100` — store actually shrinks below 100 MB.
6. `oc checkpoints clear --session XYZ --yes` — that session's store wiped.
7. `pytest tests/test_worktree_include.py tests/test_rewind_store_prune.py tests/test_checkpoint_admin.py tests/test_cli_checkpoints.py tests/test_cli_worktrees.py tests/test_auto_checkpoint_prune.py` — all green.
8. `pytest` (full suite) — no regressions.
9. `ruff check .` — clean.
10. Auto-prune fires once on first save, never twice within `min_interval_hours`.
11. `git diff main..HEAD --stat` — no files in `channels/`, `dispatch/`, `cli_gateway*`, `cli_pair.py`, or any messaging adapter.

---

## Out-of-scope follow-ups (honest deferrals)

These are explicitly NOT in this PR but are tracked for future work:

- Per-pattern directives in `.worktreeinclude` (e.g. `copy: foo`, `symlink: bar`). Add when a real use case appears.
- Compression of checkpoint dirs. Disk is cheap; YAGNI.
- Telemetry export of prune sweeps. Wire into existing observability if/when that lands.
- Subagent-store-aware `oc checkpoints status` per-subagent breakdown. Currently aggregated under each session — sufficient.
- Migrating `RewindStore` to a shadow git store (Hermes shape). Working alternative; switch is YAGNI.

---

## Rollout

- Single PR. Branch: `feat/worktree-checkpoint-hygiene-2026-05-08` off `main`.
- ~50 tests added. Zero existing tests modified beyond imports.
- All defaults preserve current behavior unless coding-harness extension is loaded.
- Auto-prune defaults `enabled=True` but `min_interval_hours=24` — first-run users see one prune sweep at most per day.
- After merge: monitor logs for prune sweep failures; if any, hotfix.
