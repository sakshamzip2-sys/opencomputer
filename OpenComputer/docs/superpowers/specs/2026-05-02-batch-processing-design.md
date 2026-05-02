# Batch Processing Capability (Subsystem E)

**Date:** 2026-05-02
**Scope:** Provider-agnostic batch-job interface — submit/poll/results across any provider that supports asynchronous batch APIs.
**Status:** Implementing in `feat/batch-processing` (stacked on Subsystems A + B + C + D).

---

## 1. Problem

Several OpenComputer subsystems are non-interactive and could benefit from batch APIs:

- Auto-skill-evolution LLM judge — runs after session end
- Edge research / signal-postmortem — overnight
- Scheduled briefings across many tickers
- Bulk classification / extraction

Anthropic offers a 50% cost discount for batch processing (~1hr typical turnaround, 24h max). OpenAI offers a similar capability with a different async-file-based shape. Other providers don't support at all.

OpenComputer has an existing `opencomputer/batch.py` CLI utility that uses Anthropic's batch API directly. There's no generic interface — anyone wanting to use batch must either copy that file's pattern or ignore the capability.

## 2. Goals & non-goals

**Goals:**

- Add `submit_batch()` and `get_batch_results()` methods to `BaseProvider`.
- Default implementations raise `BatchUnsupportedError` — opt-in per provider.
- Anthropic provider implements both natively, composing with Subsystems B (effort) and C (response_schema).
- Provider-agnostic dataclasses `BatchRequest` and `BatchResult` in `plugin_sdk`.
- Tests covering default-raises + Anthropic native + composition with B/C.

**Non-goals (deferred):**

- OpenAI batch implementation — different async-file-based shape; needs its own design pass.
- Refactoring `opencomputer/batch.py` CLI to use the new interface — separate decision; the CLI's `BatchRequest`/`BatchResult` types coexist with the new generic ones.
- Wiring batch into specific subsystems (skill-evolution judge, edge research, briefings) — each is its own decision.
- Cost-guard integration (50% discount accounting).
- Polling helpers — caller owns the polling loop.

## 3. Approach

### 3.1 Plugin SDK additions

`plugin_sdk/provider_contract.py`:

```python
class BatchUnsupportedError(NotImplementedError):
    """Provider doesn't support batch."""

@dataclass(frozen=True, slots=True)
class BatchRequest:
    custom_id: str
    messages: list[Message]
    model: str
    system: str = ""
    max_tokens: int = 1024
    runtime_extras: dict | None = None       # composes with B
    response_schema: dict | None = None      # composes with C

@dataclass(frozen=True, slots=True)
class BatchResult:
    custom_id: str
    status: Literal["succeeded", "errored", "expired", "canceled", "processing"]
    response: ProviderResponse | None = None
    error: str = ""
```

### 3.2 BaseProvider methods (default raise)

```python
async def submit_batch(self, requests: list[BatchRequest]) -> str:
    raise BatchUnsupportedError(f"{self.name} does not support batch processing")

async def get_batch_results(self, batch_id: str) -> list[BatchResult]:
    raise BatchUnsupportedError(...)
```

Concrete defaults — no abstract methods. Backwards compatible: existing providers that don't override get the canonical "unsupported" error.

### 3.3 Anthropic implementation

Translates `BatchRequest` → Anthropic's batch entry shape (custom_id + params). Each request can carry its own `runtime_extras` (effort) and `response_schema`, composed exactly like the non-batch path.

`get_batch_results` checks `processing_status`:
- `"in_progress"` → return single placeholder `BatchResult(status="processing")`
- otherwise stream native results via `client.messages.batches.results(batch_id)` and translate

### 3.4 Generic-by-design checklist

- ✅ Methods on `BaseProvider` — every provider can implement
- ✅ Default raises `BatchUnsupportedError` — clear error for unsupported providers
- ✅ `BatchRequest`/`BatchResult` in `plugin_sdk` — universal types
- ✅ Composes with Subsystems B (per-request effort) and C (per-request schema)
- ✅ Future OpenAI / Llama / Kimi batch implementations are per-provider additions, no core changes

## 4. Components & file map

| File | Change |
|---|---|
| `plugin_sdk/provider_contract.py` | NEW: `BatchUnsupportedError`, `BatchRequest`, `BatchResult`. MODIFY: `BaseProvider.submit_batch`, `BaseProvider.get_batch_results` (concrete defaults that raise). |
| `plugin_sdk/__init__.py` | Re-export new types. |
| `extensions/anthropic-provider/provider.py` | NEW: `submit_batch`, `get_batch_results` overrides composing with effort + schema. |
| `tests/test_batch_capability.py` | NEW: default-raises + Anthropic translation + composition with B/C + processing/succeeded states. |

## 5. Acceptance criteria

1. Full pytest suite green.
2. Ruff clean.
3. New tests cover: default raises, dataclass shapes, Anthropic submit translation, B/C composition in batch entries, get_batch_results processing-state placeholder, get_batch_results succeeded entries.
4. BC preserved — existing `opencomputer/batch.py` types coexist with new `plugin_sdk` types in distinct namespaces; no behavior change to existing CLI.

## 6. Out of scope (follow-up PRs)

- OpenAI batch implementation
- `opencomputer/batch.py` CLI refactor to use new interface
- Wire batch into auto-skill-evolution judge, edge research pipeline, scheduled briefings
- Cost-guard 50%-discount accounting
- Llama/Ollama/Kimi/DeepSeek native batch implementations
