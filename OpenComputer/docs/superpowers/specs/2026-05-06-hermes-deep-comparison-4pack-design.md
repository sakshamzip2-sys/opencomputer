# Hermes Deep Comparison — Operational Hardening 4-Pack

**Date:** 2026-05-06
**Driver doc:** `OpenComputer/docs/refs/hermes-agent/2026-05-06-deep-comparison.md`
**Branch:** `feat/hermes-deep-comparison-2026-05-06`

## Why this scope (and not the doc's TL;DR)

The deep-comparison doc's TL;DR recommended five priority items
(`S2`/`S1`/`S3`/`A2`/`S4`). Survey on 2026-05-06 found **all five are already
shipped** — the doc itself is one day stale.

| TL;DR item | Status | Where |
|---|---|---|
| S1 — tool-result middleware | ✅ shipped | `plugin_sdk/hooks.py:54` registers `TransformToolResult`; `loop.py:3644 + 4007` wires `_maybe_transform_tool_result`; `extensions/screen-awareness/plugin.py:204` is a working example. |
| S2 — credential-pool rotation + cooldown | ✅ shipped | `opencomputer/agent/credential_pool.py` ships 4 strategies (`fill_first`/`round_robin`/`random`/`least_used`), JWT auto-refresh, sha256-id logging, quarantine + cooldown. PR #413 closed it. |
| S3 — hooks management CLI | ✅ shipped | `opencomputer/cli_hooks.py` (PR #474). |
| A2 — Edge TTS + Groq STT | ✅ shipped | `opencomputer/voice/edge_tts.py` + `voice/tts_edge.py` + `voice/groq_stt.py`. |
| S4 — backup + profile clone/export/import | ✅ shipped | `opencomputer/cli_backup.py` (PR #474) + `cli_profile.py` (Phase 14.H, PR #446 + dry-run #454). |

Plus several Tier-B items already shipped:

- B6 worktree-per-session: wired via `oc code --worktree` (`cli.py:2082`).
- B2 insights: `cli_insights.py` exists with time/count slice; cost columns
  explicitly deferred at `cli_insights.py:13-19`.

Truly-pending high-leverage items from the doc:

- **B3** — `error_classifier` typed taxonomy
- **B5** — `retry_utils` consolidation
- **B4** — `usage_pricing` per-call cost recording
- **A3** — Mem0 memory backend

These four compose:
- B3 typed errors → B5 retry decisions become correct.
- B4 cost recording → unblocks B2's deferred cost columns.
- A3 — net-new alternative memory backend (only one with genuine
  differentiation per the doc; rest of the 7 backends are deliberately
  deferred to demand-only).

## Scope (this PR)

### B3 — `opencomputer/agent/error_classifier.py`

```python
class ErrorCategory(StrEnum):
    RATE_LIMITED = "rate_limited"   # 429; retryable with backoff
    AUTH         = "auth"           # 401, 403; rotate key, then fail
    QUOTA        = "quota"          # plan/billing exceeded; fatal
    TIMEOUT      = "timeout"        # asyncio.TimeoutError; retryable
    NETWORK      = "network"        # ConnectionError/OSError; retryable
    BAD_REQUEST  = "bad_request"    # 400, 422; fatal — bug in caller
    SERVER       = "server"         # 5xx; retryable
    UNKNOWN      = "unknown"        # default — log + retry once

def classify(exc: BaseException) -> ErrorCategory: ...
def is_retryable(category: ErrorCategory) -> bool: ...
```

Classification dispatch is structural (status code + exception class name)
so it works across SDKs (anthropic, openai, httpx, urllib3, asyncio) without
importing them — just inspects `exc.__class__.__name__` and any `status_code`
/ `response.status_code` / `code` attribute. Provider-agnostic.

### B5 — `opencomputer/agent/retry_utils.py`

```python
async def retry(
    fn: Callable[..., Awaitable[T]],
    *args,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    jitter: float = 0.5,
    retryable_categories: frozenset[ErrorCategory] | None = None,
    on_attempt: Callable[[int, BaseException, ErrorCategory], None] | None = None,
    **kwargs,
) -> T: ...

def with_retry(**kwargs):
    """Decorator form. Same params."""
    ...
```

Default retryable: `{RATE_LIMITED, TIMEOUT, NETWORK, SERVER}`. Exponential
backoff: `min(base * 2^n, max) ± jitter`.

Migration:
- `CredentialPool.with_retry` keeps its signature (it has unique key-rotation
  semantics — different concept). Reuses `error_classifier.classify` to make
  AUTH/RATE-LIMIT decisions explicit.
- Channel `_send_with_retry` (in `plugin_sdk/channel_contract.py:389`) keeps
  signature; internally delegates to `retry_utils.retry` via opt-in flag —
  default behavior preserved (no breaking change for adapter overrides).

### B4 — `opencomputer/agent/usage_pricing.py`

Wires `cost_guard.compute_call_cost` (already exists) into
the agent loop:

1. **Schema migration v12 → v13** (`opencomputer/agent/state.py`): new `llm_calls` table. (Current `SCHEMA_VERSION = 12`; this PR adds `_migrate_v12_to_v13`.)
    ```sql
    CREATE TABLE IF NOT EXISTS llm_calls (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL,
        ts              REAL NOT NULL,
        provider        TEXT NOT NULL,
        model           TEXT NOT NULL,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        cost_usd        REAL,            -- nullable when pricing unavailable
        batch           INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id);
    CREATE INDEX IF NOT EXISTS idx_llm_calls_ts      ON llm_calls(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_llm_calls_model   ON llm_calls(model);
    ```
2. `SessionDB.record_llm_call(session_id, *, provider, model, input_tokens, output_tokens, batch=False)` method computes cost lazily via `compute_call_cost`.
3. `loop.py` post-LLM-call: after `resp.usage` is read (around line 3389 +
   the streaming path), call `state.record_llm_call(...)`. Idempotency by
   construction — recorded once per actual call, not per retry.

### B2 follow-up — `oc insights cost`

`cli_insights.py` gets a new subcommand:
```
oc insights cost [--days 7] [--by-model|--by-session|--by-day]
```

Reads `llm_calls`. Renders Rich table. Falls back gracefully with friendly
message when zero rows (haven't done any LLM calls in window or schema is
fresh).

### A3 — `extensions/memory-mem0/`

```
extensions/memory-mem0/
├── plugin.json        # min_host_version, declared activation, MEM0_API_KEY hint
├── plugin.py          # register(api) → registers Mem0Provider
├── provider.py        # Mem0Provider(MemoryProvider) — system_prompt_block / on_pre_compress / on_session_end
└── README.md
```

- Default OFF (manifest declares activation but plugin enable required).
- `MEM0_API_KEY` (cloud) or `MEM0_BASE_URL` (self-hosted/local) env vars.
- `MEM0_USER_ID` derives from active profile (mirrors Honcho's host_key pattern).
- `mem0ai` dep gated behind `pip install opencomputer[mem0]` extra.
- Graceful degrade: if `mem0ai` not installed, plugin logs warning at register
  and falls back to no-op (returns empty system_prompt_block, swallows ingest).

## Ship-with-callsite checklist (per memory rule)

| Module | Callsite |
|---|---|
| `error_classifier.py` | imported by `retry_utils.py` (decision logic) and `credential_pool.py` (auth-vs-rate-limit branching) |
| `retry_utils.py` | imported by channel adapters (opt-in via `_send_with_retry` flag) |
| `usage_pricing.py` | imported by `loop.py` after `resp.usage` is read |
| `record_llm_call` | called from `loop.py` non-streaming path + streaming finalize |
| `cli insights cost` | wired into `cli.py` via existing `app.add_typer(insights_app)` |
| Mem0 provider | `register()` calls `api.register_memory_provider(...)` |

## Tests (TDD, scoped per item)

- `tests/agent/test_error_classifier.py` — table-driven over fake exceptions
  with status codes + names; asserts category + retryability.
- `tests/agent/test_retry_utils.py` — backoff timing, attempt counting,
  exhaustion behavior, retryable filter.
- `tests/agent/test_usage_pricing.py` — schema migration, record_llm_call,
  cost = None when pricing unknown.
- `tests/test_cli_insights_cost.py` — populate fake llm_calls, run CLI,
  assert table contents.
- `tests/extensions/test_memory_mem0_plugin.py` — register without mem0ai
  installed (graceful degrade), with mem0ai installed (calls mem0 client).

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Changing CredentialPool retry semantics breaks 429 handling | Don't change CredentialPool's outer signature; only delegate inner classify. Existing tests must continue to pass. |
| `record_llm_call` double-counts on retries | Record only when `resp.usage` is populated (post-success); retry path raises before that. |
| `mem0ai` import explodes existing test suite | Optional dep behind `[mem0]` extra; CI doesn't install by default; tests use mock. |
| Schema migration on busy SQLite | Migration is single ALTER… er, single CREATE TABLE — atomic. Same pattern as existing v5/v6/v7 migrations. |
| Two parallel sessions touch state.py migration | SQLite WAL + IF NOT EXISTS guards make it safe. |
| Insights `--cost` shows zero-cost on missing pricing | Show `—` placeholder for None values, not 0.0 (avoids misleading total). |

## Consolidation note

`opencomputer/gateway/dispatch.py` already has ad-hoc 429/`RateLimitError`/
`AuthenticationError`/`PermissionDeniedError` checks. This PR migrates that
inline string-comparison block to call `error_classifier.classify(exc)` so
there is one classifier per repo.

## Out of scope (deferred — explicit)

- **A1 — Web dashboard polish.** 5-7 days; not single-PR scope.
- **B1 — CDP attach.** Browser-test heavy; needs a separate session focused on
  manual verification.
- **Mem0's full feature surface** (vector ranking, agent memories): we ship
  `MemoryProvider` glue; deeper Mem0 features layer on later.
- **Per-call cost surfacing in chat UI banner.** Cost is recorded; UI surface
  defers to insights CLI for now.
- **`tests/__pycache__/test_browser_control_cdp_attach.cpython-313*.pyc`** —
  stale .pyc with no .py source. Delete in cleanup; not load-bearing.

## Self-review (post-write)

- [x] No "TBD" or vague placeholders.
- [x] Schema change is additive (new table, no column drops).
- [x] Each module has a documented callsite.
- [x] Mem0 dep is optional, no import-time crash.
- [x] Each item has a test scope.
- [x] Risk register covers parallel-session edge cases.
