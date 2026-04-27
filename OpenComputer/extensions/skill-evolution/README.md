# Skill Evolution (auto-skill-extraction)

Auto-extract reusable skills from successful OpenComputer sessions.
**Default OFF — opt-in only. No raw transcript ever leaves the local box.**

## What this does

When enabled, a small subscriber listens to the F2 typed event bus for
`SessionEndEvent` records. For each session it runs a two-stage filter
(cheap heuristic → Haiku LLM judge) and, on a match, calls a three-step
extractor pipeline that produces a SKILL.md candidate plus a
`provenance.json` sidecar. The candidate is staged under
`<profile_home>/skills/_proposed/<auto-name>/` for the user to review
with `oc skills review`. Nothing auto-publishes.

## What this does NOT do

| Thing | Status |
|---|---|
| Send any data to a network destination | No |
| Persist raw session transcripts on disk | No |
| Auto-publish a generated skill (no review step) | No |
| Run when paused or disabled | No |
| Run when sensitive-app filter matches the session | No |
| Train any model on collected data | No |
| Emit anything outside `<profile_home>/skills/_proposed/` | No |

The "no network" rule is enforced by
`tests/test_skill_evolution_no_egress.py` — a CI guard that AST-scans
this directory for HTTP-client imports. The "no raw transcript on disk"
rule is enforced by `tests/test_skill_evolution_no_raw_transcript.py`.
Both are contract breaks, not just code changes; flipping either
requires updating the deny-list, this README, and the CHANGELOG.

LLM calls go through the existing provider plugins (anthropic-provider,
etc.) which own all networking. The skill-evolution source itself
imports zero HTTP client libraries.

## Privacy contract

| Field captured | Storage | Where it goes |
|---|---|---|
| Session ID | `provenance.json` | Local disk only — `_proposed/<name>/` |
| Confidence score (judge) | `provenance.json` | Local disk only |
| Generated-at timestamp | `provenance.json` | Local disk only |
| Truncated source summary (≤500 chars, redacted) | `provenance.json` | Local disk only |
| User messages, tool calls, raw transcript | Never persisted | Held only in-memory by the LLM call, then dropped |
| LLM intent / procedure / trigger output | Run through PII regex + sensitive-filter, then written into `SKILL.md` | Local disk only |

`provenance.json` is metadata-only. It MUST NOT contain `messages`,
`transcript`, `tool_calls`, `raw_session`, or `user_messages` keys —
the privacy contract test enforces this on every CI run.

The extractor runs two redaction passes on every LLM response:

1. **Caller filter.** An optional `sensitive_filter` callable can mark
   text as too sensitive to keep. Matches are replaced with `<redacted>`.
2. **Built-in PII regex.** Credit-card-shaped digit groups and SSN-shaped
   `XXX-XX-XXXX` strings are replaced with `<redacted-pii>`.

If after redaction the body is empty / sentinel-only / shorter than
`_MIN_BODY_LEN` (20 chars of real content), the extractor returns
`None` and no candidate is staged.

## Sensitive-app filter integration

Skill-evolution shares the ambient-sensors sensitive-app list. When the
session's foreground-app trail intersected the sensitive list (password
managers, banking, healthcare, secure messaging, etc.), the heuristic
short-circuits to "skip" before any LLM is invoked. Override the list
the same way ambient does:

```
<profile_home>/ambient/sensitive_apps.txt
```

(One regex per line. `#`-prefixed lines are comments.)

## How to use

```bash
# Enable (opt in)
opencomputer skills evolution on

# See current state
opencomputer skills evolution status

# Disable
opencomputer skills evolution off

# Review staged candidates
opencomputer skills review

# Accept one
opencomputer skills accept <auto-name>

# Reject one
opencomputer skills reject <auto-name>

# List everything (active + proposed)
opencomputer skills list
```

## Platform support

Cross-platform — pure Python, no OS-specific code. Anywhere the
gateway runs (macOS / Linux / Windows), the subscriber runs.

## Troubleshooting

**Subscriber not starting after `evolution on`.**
Three things to check:
1. The gateway daemon is up (`opencomputer gateway`).
2. State actually flipped — `opencomputer skills evolution status`
   should report `enabled=true`.
3. `opencomputer doctor` — look for `skill_evolution` lines.

**Candidates piling up unreviewed.**
The extractor stages new candidates whenever a session passes both
filters; nothing auto-prunes within the review window. Run
`opencomputer skills review` to walk through them, or call
`prune_old_candidates(profile_home, max_age_days=90)` to drop
candidates older than the threshold (T4 ships a 90-day default).

**Candidate looks degenerate / sentinel-only.**
The redactor decided the LLM output was too sensitive to keep. The
extractor returns `None` in that case so no SKILL.md is staged — if
you're seeing degenerate candidates anyway, check the
`sensitive_filter` wiring; a passthrough filter will let through
content the regex layer didn't catch.

**Cost guard denied the extraction.**
`opencomputer status` will show the daily cost ceiling. The subscriber
silently drops candidates when the budget is exhausted; cost is
preferred over surprise spend.

## Disabling completely

`opencomputer skills evolution off` flips the `enabled` flag. The
subscriber unsubscribes within one tick.

If you don't trust the flag (e.g. moving to a different machine):

```
rm -rf <profile_home>/skills/_proposed/
```

The subscriber defaults to disabled when state is missing or
unreadable. Active (already-accepted) skills under
`<profile_home>/skills/<name>/` are NOT touched by `evolution off` —
they're real skills now, not candidates.

## Future phases (not in v1)

This is Phase 1. The framework can grow extensions via the same plugin
pattern. Each future phase ships its own opt-in flag + privacy
contract:

- **Phase 2**: standalone daemon (`oc skills evolution daemon`) for
  hosts without a gateway.
- **Phase 3**: cross-session pattern aggregation — needs explicit
  go-ahead; involves multi-session memory.
- **Phase 4**: opt-in auto-publish for high-confidence candidates —
  requires the user to explicitly raise the auto-publish threshold;
  default stays "review-only".

Each phase is a separate PR and a separate opt-in.
