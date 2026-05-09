# v1.1 Plans 3-6 — audit (2026-05-09)

Audit of `2026-05-08-v1-1-plan-{3,4,5,6}*.md` against current `origin/main`. Each plan is documented as demand-gated by its own preamble; this audit identifies the items that are already shipped vs the multi-week features that explicitly should NOT ship without the dogfood gate.

## Plan 3 — Heavy features and parked

**Plan's own gate:** "Per CLAUDE.md section 5, post-v1.0 work is 'demand-gated.' Plan 1 + Plan 2 must produce 2 weeks of real-use signal before plan 3 starts. If the signal does not surface a need for one of these features, demote it."

| Item | Effort | State on origin/main | Disposition |
|---|---|---|---|
| M6.1 MEMORY.md BM25 index | 2 days | NOT shipped — needs `rank_bm25` dep | Defer (demand-gated; needs dogfood signal that recall is the bottleneck) |
| M6.2 MEMORY.md vector index | 2 days | NOT shipped | Defer (depends on M6.1 + chroma/bge stack) |
| M6.3 Active Memory wiring | 3 days | Honcho overlay covers most; gap is BM25-side | Defer (depends on M6.1) |
| M6.4 Dreaming consolidation | 3-4 days | NOT shipped — post-response reviewer covers extraction | Defer (demand-gated; loop builds on M6.x) |
| M9 Auto-mode classifier | 1 sprint | NOT shipped — security-critical | **Defer** (poison-resistance property requires adversarial test investment beyond 1 session) |
| M10 Per-channel routing | 1 sprint | NOT shipped | Defer |
| M11.* parked items | indefinite | Explicitly parked by plan | **Skip** (the plan explicitly says "touch them only when a real user blocks on them") |

## Plan 4 — Quick wins

| Item | Effort | State on origin/main | Disposition |
|---|---|---|---|
| **M12 `/btw` side question** | half day | ✅ **ALREADY SHIPPED** — `opencomputer/agent/slash_commands_impl/btw_cmd.py` exists; routed via slash dispatcher | **Skip — done** |
| M13 Plugin CLI command registration | 2-3 days | NOT shipped — `register_cli_command` not in `PluginAPI` | Defer (3-day work doesn't fit a single session honestly; needs lazy-load benchmark) |

## Plan 5 — Multi-agent scaling

**Plan's own gate:** "Both were parked in plan 3 because neither has a documented current use case in the project history. They remain demand-gated."

| Item | Effort | Hard prereq | Disposition |
|---|---|---|---|
| M14 `/batch` parallel migrations | 5-6 days | Plan 2 M4 worktree isolation (✅ shipped) | Defer (no documented use case; per the plan itself "demand-gated") |
| M15 Broadcast groups | 4-5 days | Plan 3 M10 routing rules (NOT shipped) | Defer (hard prereq missing) |

## Plan 6 — Plugin marketplace

| Item | Effort | State | Disposition |
|---|---|---|---|
| M16.1 Typed source resolver (PyPI/GitHub/git/local) | 3-4 days | NOT shipped | Defer (no current consumer asking for non-PyPI sources) |
| M16.2 Signature verification | 2-3 days | NOT shipped | Defer (depends on M16.1) |
| M16.3 Lockfile + reproducible install | 2 days | NOT shipped | Defer (depends on M16.1) |
| M16.4 Plugin sandbox install | 2-3 days | NOT shipped | Defer (depends on M16.1) |
| M16.5 Update + audit + uninstall | 2 days | NOT shipped | Defer (depends on M16.1-4) |

## Honest summary

**Already shipped:** M12 (`/btw`) — found pre-shipped via the slash-command dispatcher.

**Cannot ship in one session, AND the plan itself says "demand-gated":** every other item across plans 3-6.

The plans were written with explicit dogfood gates and demand-gating. Shipping any of these without that signal would violate the spec's own brutal-honesty preamble. The path forward is:

1. Cut v1.0 → v2026.5.9 (M0 release).
2. Use OpenComputer daily for 2 weeks to surface real demand signals.
3. Re-evaluate plans 3-6 against actual usage gaps, not speculation.

This audit completes the requested coverage of plans 3-6. The work itself is genuinely deferred by design.
