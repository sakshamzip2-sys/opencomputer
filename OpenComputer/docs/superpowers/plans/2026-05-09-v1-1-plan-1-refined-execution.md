# v1.1 Plan 1 — refined execution (2026-05-09)

Status: ready to execute.
Origin: refines `2026-05-08-v1-1-plan-1-foundation-and-cleanup.md` after a brainstorm-phase audit against the actual repo state.

## Brainstorm-audit findings

The original plan was authored 2026-05-08. Between then and now, several listed items shipped in unrelated PR waves. The 9-lens audit and grep verification produced:

| Item | Original status | Verified state | Action |
|---|---|---|---|
| M0  v1.0.0 tag | "blocks everything" | OBSOLETE — project uses calver (`2026.5.5`); RELEASE.md explicitly forbids semver | Skip; human cuts `v2026.5.9` when ready |
| M1.1 flock on profile.yaml | "missing" | DONE — `opencomputer/profiles_lock.py` + `agent/profile_yaml.py`; `cli_bindings.py:5` says "Closes the latent profile.yaml flock tech debt" | Skip |
| M1.2 unify YAML parse | open | TRUE pending — 3 raw `yaml.safe_load` callsites at `cli_plugin.py:737`, `cli_profile.py:707`, `cli_profile.py:792` | Execute |
| M1.3 wire AgentCache | open | TRUE pending — class still has zero production callers; `aux_llm.py` / `reviewer.py` / `auxiliary_client.py` do NOT reference it | Execute |
| M1.4 per-profile `.env` | open | DONE — `cli.py:4869` "Round 4 Item 5"; `security/env_loader.py` has `load_for_profile` with stronger 0600 enforcement than the plan spec | Skip |
| M1.5 E7 demand-detection | open | DONE — `agent/loop.py:896` fires `USER_PROMPT_SUBMIT`, `plugins/demand_tracker.py:273` consumes via `scan_user_prompt` | Skip |
| M2.1 `--bare` flag | open | TRUE pending — but no current consumer; deferred per YAGNI | Defer |
| M2.2 `--output` modes | open | TRUE pending; `oc chat` only has `--headless`, no `--output` selector | Execute |
| M2.3 `--output-schema` | open | TRUE pending — but no current consumer; deferred per YAGNI | Defer |
| M3.1 permission wire | open | TRUE pending — `protocol_v2.py` lacks PERMISSION_REQUEST/RESPONSE | Execute |
| M3.2 transfer-token | open | TRUE pending — `cli_session.py:347::session_resume` only prints copy-paste hint; no signed-token cross-process resume; no current wire client demand | Defer |
| M3.3 wire ring buffer | open | TRUE pending — no `seq` on `WireEvent`, no replay path | Execute |

## 9-lens audit on the refined items

1. **Assumption-check** — All 4 remaining items are verified against grep + import tracing. No drift assumptions.
2. **Architecture stress** — M3.1 race (two clients on same session, both deny/allow): keyed on `request_id`, first wins, others 404. M3.3 overflow: `gap_warning` flag is honest about lost events.
3. **Alternative dismissal** — M1.3 cleaner alternative is to memoize at `aux_llm.complete_text` rather than wrap reviewer's call site. Pivots: cleaner contract, fewer touchpoints.
4. **Requirement gap** — M2.2 schema must include `error.code` per Hermes wire convention; aligns with existing `gateway/error_codes.py`.
5. **Composability** — All 4 items decouple (separate files, no shared state), can ship as independent PRs.
6. **Scope honesty** — M2.2 is closer to 1 day than half-day given 4 acceptance smoke paths.
7. **API surface drift** — adding `seq: int | None = None` to `WireEvent` and new optional methods to `METHOD_SCHEMAS` keeps old clients working.
8. **Failure modes** — M3.1 with no handler set: `ConsentGate.request_approval` already auto-denies; the new `WirePromptHandler` registration is opt-in.
9. **YAGNI** — applied above; M2.1, M2.3, M3.2 deferred.

## Execution plan (4 PRs)

PRs ship from worktree `.claude/worktrees/v1-1-plan1-2026-05-09` on branch base `feat/v1-1-plan1-2026-05-09` (forks per item). Each PR runs touched-feature pytest + cross-cutting pytest + ruff before push.

### PR-A — M1.2 unify YAML parse paths
- Branch: `fix/v1-1-yaml-parse-unify-2026-05-09` off `origin/main`.
- Add helper `opencomputer.agent.config_store.load_yaml_dict(path, *, allow_unknown_keys=False)` that wraps `yaml.safe_load` with size cap + key validation hook.
- Migrate 3 callsites (`cli_plugin.py:737`, `cli_profile.py:707`, `:792`) to the helper. Lenient sites pass `allow_unknown_keys=True`.
- Tests: `tests/test_yaml_parse_unified.py` covering strict-fails-on-unknown, lenient-warns-on-unknown, both share parser.
- Acceptance: `grep -rn 'yaml\.safe_load(' opencomputer/cli_*.py` returns nothing.
- Effort: ~30 min.

### PR-B — M1.3 wire AgentCache into reviewer aux_llm
- Branch: `feat/v1-1-agent-cache-wired-2026-05-09` off `origin/main`.
- Add module-level `AgentCache` instance to `opencomputer.agent.aux_llm`. Wrap `complete_text` lookup keyed on `(model, system_hash, prompt_hash)` per cache spec. Add `bypass_cache=True` kwarg for callers that need fresh reads (judge_reviewer, dreaming).
- Cache stats: extend `cli_usage.py` with `cache_hits` / `cache_misses` row.
- Tests: identical-prompt twice → 1 upstream call; `bypass_cache=True` → always upstream; LRU eviction at `DEFAULT_AGENT_CACHE_MAX`.
- Effort: ~30 min.

### PR-C — M2.2 `--output text|json|stream-json` on oc chat
- Branch: `feat/v1-1-output-modes-2026-05-09` off `origin/main`.
- Files: `opencomputer/cli.py` (new `--output` Typer option on `chat`), `opencomputer/headless.py` (new `OutputMode` enum + accessor).
- `text` (default): current behavior.
- `json`: emit single JSON object on stdout at end of run: `{session_id, num_turns, total_input_tokens, total_output_tokens, total_cost_usd, final_message, error?, exit_code}`. Aggregate via existing `LLMCallEvent`.
- `stream-json`: tee `llm_events.jsonl` writes to stdout in real time (NDJSON). File write stays intact.
- Tests: `tests/test_output_modes.py` covering each mode against mock provider; JSON parses; stream-json arrives before turn end.
- Skip the original plan's `--bare` prereq; tests use `--headless`.
- Effort: ~1 hr.

### PR-D — M3.1 + M3.3 wire protocol completeness
- Branch: `feat/v1-1-wire-permission-replay-2026-05-09` off `origin/main`.
- M3.1 — permission events:
  - `gateway/protocol_v2.py`: add `EVENT_PERMISSION_REQUEST = "permission.request"` + `METHOD_PERMISSION_RESPONSE = "permission.response"` with `PermissionRequestPayload` + `PermissionResponseParams` strict pydantic models.
  - `gateway/wire_server.py`: route `permission.response` RPC into `ConsentGate.resolve_pending`.
  - `gateway/dispatch.py`: add `WirePromptHandler` that emits a `permission.request` wire event when `ConsentGate.request_approval` fires under a wire-bound session.
- M3.3 — ring buffer:
  - `gateway/wire_server.py`: per-session `collections.deque(maxlen=200)`. Add monotonic `seq` field (Optional[int]) to `WireEvent`.
  - `hello` accepts optional `last_event_seq`; replay `(last_event_seq, current]`. Overflow yields `gap_warning: true` in `HelloResult`.
- Tests: `test_wire_permission_flow.py` (two clients, one approves), `test_wire_permission_timeout.py` (auto-deny), `test_wire_permission_concurrent.py` (independent request_ids), `test_wire_reconnect.py` (resume after disconnect), `test_wire_reconnect_overflow.py` (gap_warning when >200).
- Effort: ~3 hr.

## Acceptance gates (run before each push)

- `pytest tests/<targeted>` — touched-module test files.
- `pytest tests/ -x -k "not browser and not voice and not honcho"` — full suite minus pre-existing flakes from the registry.
- `ruff check opencomputer/ plugin_sdk/ tests/`.
- Per-PR squash-merge from GitHub UI (no admin bypass) once CI is green.

## Refused / deferred

- M2.1 `--bare`, M2.3 `--output-schema`, M3.2 transfer-token: YAGNI — defer until first consumer.
- M0 v1.0.0 tag: obsolete; project is on calver. Today's release would be `v2026.5.9` if the operator chooses.
- Bundling M1.2 + M1.3: original plan's atomic-bisect rule respected; one PR each.
- M3.1 + M3.3 bundled: same files (`protocol_v2.py`, `wire_server.py`); bisecting to commit-level still works.
