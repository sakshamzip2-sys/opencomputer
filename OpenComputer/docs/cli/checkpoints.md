# `oc checkpoints` — RewindStore hygiene

OpenComputer's coding-harness extension takes filesystem snapshots
("checkpoints") before each destructive tool call (`Edit`, `MultiEdit`,
`Write`, `Bash`). They're stored at
`~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/` and back
the `/rollback`, `/undo`, and `/checkpoint` slash commands.

`oc checkpoints` lets you observe and prune that store.

## `oc checkpoints status`

Prints a Rich table with one row per session store + global totals.

```
$ oc checkpoints status
                  checkpoint stores
┏━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ session_id  ┃ count ┃   size ┃ oldest              ┃ newest              ┃ subagents ┃ last_prune          ┃
┡━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ 20260508_…  │    47 │ 12.3 MB│ 2026-05-01T08:12:01 │ 2026-05-08T03:04:55 │         0 │ 2026-05-07T00:00:01 │
└─────────────┴───────┴────────┴─────────────────────┴─────────────────────┴───────────┴─────────────────────┘

total: 47 checkpoints across 1 sessions = 12.3 MB
```

Subagent dirs roll up into the parent session's `count` and `size`. The
`subagents` column shows how many distinct subagent stores exist for
that session.

## `oc checkpoints prune`

Apply a retention policy. Defaults come from `Config.checkpoints` (see
`agent/config.py`):

| Flag | Default (config) | Effect |
|---|---|---|
| `--older-than DAYS` | `retention_days = 30` | drop checkpoints older than N days |
| `--max-size MB` | `max_total_size_mb = 1000` | global aggregate size cap; oldest-first eviction |
| `--max-count N` | `max_snapshots = 50` | per-session cap |
| `--session SID` | (all) | apply only to one session |
| `--no-delete-orphans` | (default delete) | preserve dirs with corrupt `meta.json` |
| `--dry-run` | (off) | print would-delete report; no I/O |

Example: hard-cap to 200 MB and drop anything > 14 days old; preview first:

```
oc checkpoints prune --older-than 14 --max-size 200 --dry-run
```

If the dry-run report looks right, drop the `--dry-run` flag to apply.

### Atomicity

Each scheduled checkpoint is renamed into `<store>/.pending_delete/<id>`
before the final `rmtree`. If the process crashes mid-prune, the next
prune sweeps the leftover `.pending_delete/*` automatically — no
manual recovery needed.

## `oc checkpoints clear`

Wipes checkpoint dirs (preserves `.last_prune` markers).

```
oc checkpoints clear            # interactive: prompts for confirmation
oc checkpoints clear --yes      # skip prompt
oc checkpoints clear --session 20260508_abcdef --yes  # one session only
```

In a non-interactive environment (CI, piped input) the command refuses
without `--yes` and exits with code 2.

## Auto-prune

When the coding-harness extension is loaded, the `auto_checkpoint`
PreToolUse hook also schedules a background prune sweep on first fire
per process — subject to `checkpoints.min_interval_hours` (default
24h). The prune is scheduled AFTER the save lands so the two never
race on the store directory.

Disable via:

```yaml
checkpoints:
  auto_prune: false
```

Or tighten the interval:

```yaml
checkpoints:
  min_interval_hours: 6   # sweep up to 4× per day
```

## Storage layout

```
~/.opencomputer/harness/
  <session_id>/
    rewind/
      <checkpoint_id>/
        meta.json
        files/
          path-with-slashes-replaced-by-double-underscore
      [subagents/<subagent_id>/<checkpoint_id>/...]
      .last_prune                      ← auto-prune marker (mtime tracked)
      .pending_delete/<id>/...         ← atomic-delete staging (transient)
```

`meta.json` schema:

```json
{
  "id": "16-char-hex",
  "label": "before Edit",
  "created_at": "2026-05-08T03:04:05+00:00",
  "paths": ["src/foo.py", "tests/test_foo.py"],
  "excluded_files": ["large.bin"]
}
```

`excluded_files` is files that exceeded `max_file_size_mb` at snapshot
time and were therefore excluded from the snapshot. The hash is
computed only over included files.

## Files referenced

- `extensions/coding-harness/rewind/store.py` — `RewindStore`
- `extensions/coding-harness/rewind/checkpoint.py` — `Checkpoint`
- `extensions/coding-harness/hooks/auto_checkpoint.py` — PreToolUse hook
- `opencomputer/checkpoint_admin.py` — `iter_stores`, `prune_all`, `clear_all`
- `opencomputer/cli_checkpoints.py` — Typer subapp
- `opencomputer/agent/config.py` — `CheckpointsConfig`
