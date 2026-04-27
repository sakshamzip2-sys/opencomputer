---
name: profile-scraper
description: Build a structured profile of the user from their laptop — files, git, browser, system identity. Schema-driven, denylist-respecting, diff-aware refresh. Use when the user says "scrape my laptop", "learn about me", "build a profile of me", or to refresh an existing profile snapshot.
---

# Profile Scraper

Builds and maintains a structured profile of the user across ~50 sources on
their laptop. Output is canonical: every fact has a `source`, `confidence`,
and `timestamp`. Snapshots are versioned at `<profile_home>/profile_scraper/snapshot_<ts>.json`
so diffs are observable.

## When to use this skill

- "Scrape my laptop", "learn about me", "build a profile of me"
- "What do you know about me?" (read latest snapshot)
- "Refresh my profile"

## What gets scraped

Identity (5 sources): `$USER`, `git config`, Contacts.app `me` card, mail accounts plist, browser saved logins.

Projects (8 sources): `~/Vscode`, `~/Documents/GitHub`, `~/clean`, `~/.claude/plugins/local`, `gh repo list`, `gh starred`, recent git activity, language histogram from cloc.

Behavior (10 sources): Brave history, Chrome history, Safari history (Spotlight-fallback if locked), shell history (`~/.zsh_history`), recent files (mdfind via Spotlight), app usage (`ps aux`-derived), git commit cadence, PR review activity.

Knowledge & interests (7 sources): YouTube subscription cookie tags, RSS reader OPML, Notes.app titles (FDA-gated), bookmarks, Reading List, Pocket export.

System (5 sources): hostname, locale, timezone, hardware (`system_profiler SPHardwareDataType`), installed apps inventory.

Secrets audit (3 sources): grep for `TOKEN|API_KEY|SECRET` in `~/.zshrc` + `~/.zsh_history` + `~/.config/*` (read-only — flag, never modify).

## Denylist (NEVER read)

- `~/.ssh/*` (private keys)
- `~/Library/Messages/chat.db` (iMessage history is too sensitive without explicit consent)
- `~/Documents/Financial/*` and any `*.pdf` matching `bank|tax|invoice` heuristic
- `~/Library/Keychains/*`
- `~/.aws/credentials`
- `~/.config/gh/hosts.yml` (token storage)

## Schema

Every fact is a `ProfileFact`:
```python
{
    "field": "primary_email",
    "value": "saksham.zip2@gmail.com",
    "source": "git_config_global",
    "confidence": 1.0,
    "timestamp": 1714000000.0
}
```

## Refresh semantics

- First run: full scrape, write `snapshot_<ts>.json`.
- Subsequent runs: read previous snapshot, scrape again, **diff** — write new snapshot only if any field changed; otherwise update only the timestamp.
- Old snapshots retained (last 10) so historical changes are observable.

## Output destinations

- Structured snapshot: `<profile_home>/profile_scraper/snapshot_<ts>.json`
- Latest pointer: `<profile_home>/profile_scraper/latest.json` (symlink-style copy)
- High-confidence facts auto-written to F4 user-model graph as Identity nodes.

## CLI surface

```bash
opencomputer scrape           # default: incremental refresh
opencomputer scrape --full    # ignore previous snapshot, full re-scrape
opencomputer scrape --diff    # compare latest two snapshots, print changes
```

## Privacy posture

The skill writes to local disk only. No data leaves the machine. F1 consent gates:
- `profile_scraper.identity` (IMPLICIT) — system + git config
- `profile_scraper.projects` (IMPLICIT) — repo listings, no contents
- `profile_scraper.behavior` (EXPLICIT) — browser + shell history
- `profile_scraper.knowledge` (EXPLICIT) — Notes / RSS / bookmarks
- `profile_scraper.secrets_audit` (EXPLICIT) — grep for leaked tokens (read-only)

Each can be revoked via `opencomputer consent revoke profile_scraper.<id>`.
