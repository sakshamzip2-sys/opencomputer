# Per-Context Reasoning Effort Policy (Subsystem B)

**Date:** 2026-05-02
**Scope:** Subsystem B from the docs-1-7 review. Per-mode/subagent/model effort defaults.
**Status:** Implemented in `feat/effort-policy`. Builds on Subsystem A (PR #318).

---

## 1. Problem

After Subsystem A, the `effort` parameter is plumbed end-to-end. But every call still runs at the API default (`high` on Claude, OpenAI's default on reasoning models). This is wasteful for three concrete cases the docs explicitly call out:

- **Subagents** — Anthropic Doc 5: "such as subagents" verbatim listed as the canonical `low` use case. OpenComputer's `DelegateTool` spawns child loops at default `high` today.
- **Voice mode** — realtime voice (PR #270) is latency-bound. A thinking budget on every utterance kills round-trip; should be `low`.
- **Sonnet 4.6 chat** — Doc 5 explicitly: "Sonnet 4.6 defaults to `high` effort. Explicitly set effort when using Sonnet 4.6 to avoid unexpected latency." Should default to `medium`.

A user who hasn't run `/reasoning <level>` should get sensible defaults per context — without losing the ability to override.

## 2. Goals & non-goals

**Goals:**

- Apply per-context `reasoning_effort` defaults when the user hasn't explicitly set one.
- Subagents get `low` automatically (Doc 5 explicit guidance).
- Voice mode gets `low` (when active).
- Per-model tier defaults: Opus 4.7 → `xhigh`, Sonnet 4.6/4.5 → `medium`, OpenAI reasoning → `medium`.
- User's `/reasoning <level>` always wins.
- Provider-agnostic — works for any provider whose `*_kwargs_from_runtime` translator accepts `reasoning_effort`.

**Non-goals (deferred):**

- Voice mode CLI integration — touches `cli_voice.py`; voice integration only fires if `runtime.custom["voice_mode"]` is True, but no caller sets that flag yet. Voice CLI / realtime voice plugin will set it in a follow-up PR.
- Persona-based overrides — V2.C's plural personas could each have a per-persona effort hint. Out of scope.
- Skill-based overrides — `/code` skill could request `xhigh`. Out of scope.
- Per-model overrides at runtime — config.yaml hook to override defaults. Out of scope.

## 3. Approach — pure-function policy in core

A small pure-function module (~70 LOC) at [opencomputer/agent/effort_policy.py](../../opencomputer/agent/effort_policy.py):

```python
def recommended_effort(*, runtime: RuntimeContext | None, model: str) -> str | None:
    # Priority 1: subagent context → low
    if runtime and runtime.delegation_depth > 0:
        return "low"
    # Priority 2: voice mode → low
    if runtime and runtime.custom.get("voice_mode") is True:
        return "low"
    # Priority 3: per-model defaults
    return _model_default(model)
```

Loop integration: in [loop.py:2809](../../opencomputer/agent/loop.py#L2809), right after `_runtime_extras = runtime_flags_from_custom(self._runtime.custom)`:

```python
if _runtime_extras.get("reasoning_effort") is None:
    _policy_default = recommended_effort(
        runtime=self._runtime,
        model=model_name,
    )
    if _policy_default is not None:
        _runtime_extras["reasoning_effort"] = _policy_default
```

User-set effort wins because we only fill in when `None`.

## 4. Capability table

| Context | Recommendation | Rationale |
|---|---|---|
| `runtime.delegation_depth > 0` (subagent) | `low` | Doc 5 verbatim |
| `runtime.custom["voice_mode"] is True` | `low` | Latency-bound realtime voice |
| `claude-opus-4-7*` | `xhigh` | Doc 5: "recommended starting point for coding/agentic" |
| `claude-sonnet-4-6*`, `claude-sonnet-4-5*` | `medium` | Doc 5 latency warning |
| `o1*`, `o3*`, `o4*`, `gpt-5-thinking*` | `medium` | Sensible paid-tier default |
| Everything else | `None` (no recommendation) | API default applies |

`None` for unknown models means: don't touch `runtime_extras`. The provider-side translator either ignores `reasoning_effort` (non-reasoning models) or applies its own default.

## 5. Provider-agnostic by design

The framework is universal:

- `recommended_effort` returns OpenComputer's internal scale (`low`/`medium`/`xhigh`/etc.).
- The provider-side `*_kwargs_from_runtime` translates to native shapes:
  - Anthropic adaptive → `output_config.effort`
  - Anthropic legacy → `enabled+budget_tokens`
  - OpenAI → `reasoning_effort`
  - Future Kimi/DeepSeek/Llama-thinking → their own builders

Models that don't support reasoning (legacy Claude 3, base Llama, etc.) are unaffected — their translators ignore `reasoning_effort`.

## 6. Testing strategy

24 pure-function tests + 2 end-to-end loop integration tests:

- **Pure function:** subagent depth, voice mode, per-model defaults across Claude/OpenAI/Llama/Kimi/Ollama/DeepSeek, edge cases (None runtime, depth=3, voice_mode=False).
- **Integration:** captures `runtime_extras` at the provider-call site; verifies (a) policy default applies when user hasn't set effort, (b) user-set effort wins over policy.

## 7. Acceptance criteria

1. ✅ Full pytest suite green (26 new tests + no regressions).
2. ✅ Ruff clean.
3. ✅ Subagent dispatch (any model) gets `reasoning_effort=low` at the provider call.
4. ✅ Opus 4.7 default chat (no `/reasoning`) gets `xhigh`.
5. ✅ Sonnet 4.6 default chat (no `/reasoning`) gets `medium`.
6. ✅ User-set `/reasoning <level>` wins over policy.

## 8. Follow-ups (separate PRs)

- **Voice mode wiring** — `cli_voice.py` and `realtime_voice/` set `runtime.custom["voice_mode"] = True` when constructing the AgentLoop's runtime.
- **Persona-based effort** — per-persona effort hint in persona config (companion → `medium`, code → `xhigh`).
- **Skill-based effort** — skills can request a minimum effort tier.
- **Config-driven model overrides** — users override defaults via `~/.opencomputer/config.yaml`.
