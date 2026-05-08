# `oc worktrees` — `.opencomputer-worktrees/` management

`oc code -w` creates a fresh git worktree under
`<repo>/.opencomputer-worktrees/<id>/` so an experimental coding session
doesn't disturb the main checkout. `oc worktrees` is the admin surface
for those directories.

## `oc worktrees list`

```
$ oc worktrees list
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ session_id  ┃ branch                 ┃ path                                                ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ abc123      │ refs/heads/oc-session…│ /repo/.opencomputer-worktrees/abc123                │
└─────────────┴────────────────────────┴─────────────────────────────────────────────────────┘
```

A row marked `[unregistered]` in the branch column indicates a
leftover worktree dir on disk that's no longer tracked by git — likely
a crashed prior session. `oc worktrees clean` removes those.

## `oc worktrees clean`

```
oc worktrees clean              # remove stale (unregistered) worktrees
oc worktrees clean --dry-run    # preview which would be removed
oc worktrees clean --all        # also remove ACTIVE oc worktrees (use with care)
```

"Stale" = present on disk but not registered with `git worktree list`.
Default behavior preserves registered worktrees; pass `--all` to wipe
every entry under `.opencomputer-worktrees/` regardless.

## `oc worktrees include-preview`

Reads the project's `.worktreeinclude` (and the global
`~/.opencomputer/worktreeinclude` if `worktree.include_global_fallback`
is true) and prints what *would* be copied if a fresh `oc code -w` ran
now.

```
$ oc worktrees include-preview
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ source                                 ┃   bytes ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ /repo/.env                             │      87 │
│ /repo/.venv                            │  53,124 │
└────────────────────────────────────────┴─────────┘

total: 53,211 bytes (0.1 MB)
```

## `.worktreeinclude` syntax

Gitignore-style. One pattern per line, `#` comments, blank lines
ignored. Patterns are globs relative to repo root.

```
# .worktreeinclude
.env
.venv/
node_modules/
config/*.local.yaml
```

A pattern that resolves outside the repo root is silently rejected.

### Resolution order

1. `<repo_root>/.worktreeinclude` (project-specific)
2. `<profile_home>/worktreeinclude` (global fallback; opt-out via
   `worktree.include_global_fallback: false`)

Patterns from both are unioned; project takes precedence on duplicates.

## Caps

| Config | Default | Effect |
|---|---|---|
| `worktree.include_max_total_mb` | 1000 | hard cap on total bytes; abort above this |
| `worktree.include_max_per_file_mb` | 500 | per-file warn + skip threshold |
| `worktree.include_global_fallback` | true | also read `~/.opencomputer/worktreeinclude` |
| `worktree.include_follow_symlinks` | false | symlinks copied AS symlinks |

Total > cap aborts the worktree session with a clear error and removes
the partial worktree (a half-populated worktree is worse than a clear
failure). Per-file > cap skips just that file with a WARNING log;
other files in the include set still copy.

## Atomicity

Each file is copied to a `.<name>.tmp.<rand>` then renamed via
`os.replace`, so a crash mid-copy never leaves a partial file at the
destination path.

## Security note

`.worktreeinclude` lets you copy gitignored secrets (`.env`) into a
worktree. The worktree is on a fresh branch — if you commit the secrets
there and push, that's your responsibility, not ours. Don't add `.env`
to `.worktreeinclude` for repos where the worktree branch will be
pushed to a public remote.

## Files referenced

- `opencomputer/worktree.py` — `session_worktree` context manager
- `opencomputer/worktree_include.py` — parser + expander + copier
- `opencomputer/cli_worktrees.py` — Typer subapp
- `opencomputer/agent/config.py` — `WorktreeConfig`
