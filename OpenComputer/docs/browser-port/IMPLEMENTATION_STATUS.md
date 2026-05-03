# Browser-port — IMPLEMENTATION STATUS

> Live coordination doc. Sister sessions claim a subsystem here, update status as they progress, and link the PR when they're done.
>
> Authoritative design: [BLUEPRINT.md](BLUEPRINT.md). Reference material: [docs/refs/openclaw/browser/](../refs/openclaw/browser/).
>
> Last updated: 2026-05-03 by s4 (Wave 2 claim — W2a/W2b bundled on `feat/browser-port-wave2`; also fixed W0 PR links from #392 → #376).

---

## How to use this doc

1. **Before starting work**: edit the table below, set yourself as the Owner of one row (use a session label you'll remember — `s1`, `s2`, or your handle), set Status to `claimed`, and create the branch.
2. **As you work**: update Status (`in progress` / `blocked: <reason>` / `review`).
3. **When you open a PR**: paste the PR URL.
4. **When merged**: Status → `merged`. Update the date.
5. Don't claim multiple rows at once unless they're explicitly marked as parallelizable in the same wave (see BLUEPRINT §9).

If a row is `blocked`, write **what** it's blocked on in the row so other sessions know whether they can unblock you.

## Conventions

- **Branch naming**: `feat/browser-port-<subsystem-slug>` — e.g. `feat/browser-port-chrome`, `feat/browser-port-session`.
- **Commit prefix**: `browser-port: <subsystem>: <message>`.
- **PR title**: `Browser port — <subsystem>`.
- **Worktree** (recommended for parallel sessions): `git worktree add ../oc-bp-<subsystem> feat/browser-port-<subsystem>`.

---

## Subsystem table

| # | Subsystem | Brief | Wave | Depends on | Owner | Branch | Status | PR | Notes |
|---|---|---|---|---|---|---|---|---|---|
| W0a | `_utils/` | [BRIEF-utils](BRIEF-utils.md) | 0 | — | s2 | feat/browser-port-foundation | merged | [#376](https://github.com/sakshamzip2-sys/opencomputer/pull/376) | 24 tests; atomic_write fsync verified |
| W0b | `profiles/` | [BRIEF-01](BRIEF-01-chrome-and-profiles.md) | 0 | — | s2 | feat/browser-port-foundation | merged | [#376](https://github.com/sakshamzip2-sys/opencomputer/pull/376) | 41 tests; pull-based resolver + capabilities |
| W0c | `chrome/` | [BRIEF-01](BRIEF-01-chrome-and-profiles.md) | 0 | `profiles/`, `_utils/` | s2 | feat/browser-port-foundation | merged | [#376](https://github.com/sakshamzip2-sys/opencomputer/pull/376) | 39 tests; mocked spawn+probe; legacy no-egress guard rescoped |
| W1a | `session/` | [BRIEF-02](BRIEF-02-cdp-and-session.md) | 1 | `chrome/` | s3 | feat/browser-port-wave1 | merged | [#392](https://github.com/sakshamzip2-sys/opencomputer/pull/392) | 73 tests; CDP dedup + retry, force-disconnect, role-ref LRU, nav guard |
| W1b | `snapshot/` | [BRIEF-04](BRIEF-04-ai-and-snapshot.md) | 1 | `_utils/` | s3 | feat/browser-port-wave1 | merged | [#392](https://github.com/sakshamzip2-sys/opencomputer/pull/392) | 35 tests; Path 2 (aria) + Path 3 (Chrome MCP), 7×6 screenshot grid |
| W1c | `server_context/` | [BRIEF-05](BRIEF-05-server-and-auth.md) | 1 | `chrome/`, `profiles/` | s3 | feat/browser-port-wave1 | merged | [#392](https://github.com/sakshamzip2-sys/opencomputer/pull/392) | 37 tests; ProfileDriver injection, last_target_id fallback chain |
| W2a | `tools_core/` | [BRIEF-03](BRIEF-03-pw-tools-core.md) | 2 | `session/` | s4 | feat/browser-port-wave2 | claimed | — | the workhorse; densest subsystem |
| W2b | `server/` | [BRIEF-05](BRIEF-05-server-and-auth.md) | 2 | `session/`, `snapshot/`, `server_context/` | s4 | feat/browser-port-wave2 | claimed | — | HTTP server, auth, routes, dispatcher |
| W3 | `client/` + `tools.py` + `plugin.py` + e2e | [BRIEF-06](BRIEF-06-client-and-utils.md) (TBD) | 3 | all | — | — | not started | — | wiring + integration tests |
| W4 | `providers/base.py` + retrofit | (deferred) | 4 | W3 done | — | — | not started | — | Hermes seam; post-v0.1 |

## Pending decisions blocking specific waves

These need answers before the named wave can start (see BLUEPRINT §11):

- **W0c** (chrome/): pull-based config refresh confirmed? *Default: yes.*
- **W1b** (snapshot/): `_snapshot_for_ai` vs `aria_snapshot()` — are we shipping role-mode only for v0.1? *Recommend: yes.*
- **W2b** (server/): keep all three target modes (`host`/`sandbox`/`node`) in the schema even if only `host` is wired? *Recommend: yes — no future schema break.*
- **General**: tool name `Browser` (PascalCase) confirmed? *Recommend: yes.*

If any of the above flips, the BLUEPRINT changes first, then this doc.

## Cold-start checklist for a sister session

If you're a fresh Claude Code session that just opened this doc, do this in order:

1. Read [BLUEPRINT.md §1–§10](BLUEPRINT.md) (skim §11–§12).
2. Look at the table above; pick a row whose dependencies are `merged` (or pick Wave 0 if nothing's merged yet).
3. Edit this doc — set yourself as Owner, set Status to `claimed`.
4. Create your branch: `git worktree add ../oc-bp-<slug> feat/browser-port-<slug>`.
5. `cd` into the worktree, then read your subsystem's deep dive in [docs/refs/openclaw/browser/0X-*.md](../refs/openclaw/browser/) end-to-end.
6. (Once briefs are written) read your `BRIEF-0X-*.md`.
7. Begin work in `extensions/browser-control/<your-subsystem>/`.
8. Run tests as you go: `pytest extensions/browser-control/tests/test_<your-subsystem>_*.py`.
9. When green, open a PR with title `Browser port — <subsystem>`. Update Status here.

## Communication between sessions

- **No real-time chat between Claude sessions.** Coordinate exclusively through:
  - This doc (status updates)
  - Git commits / PRs (work product)
  - The deep-dive `.md` files (shared context)
- If you discover something the BLUEPRINT got wrong, **don't silently work around it** — open a draft PR amending [BLUEPRINT.md](BLUEPRINT.md) so other sessions don't repeat your discovery.
- If you need an answer from the orchestrator (the human user), surface it in your PR description with a `**Question:**` line. Don't block waiting for an answer if the work can proceed under a stated assumption.

## Definition of done (per subsystem)

A subsystem PR is mergeable when:

- [ ] All exported functions match the brief's contract (or the brief is amended in the same PR)
- [ ] Unit tests cover the load-bearing flows (no need for 100% line coverage)
- [ ] No imports from `opencomputer/*` — only from `plugin_sdk/*` (the SDK boundary test passes)
- [ ] No new entries in the "Bugs we don't reproduce" table from BLUEPRINT got reintroduced
- [ ] `ruff check` clean
- [ ] If your subsystem touches the doctor surface, doctor row passes / fails meaningfully
- [ ] STATUS table updated, PR linked
