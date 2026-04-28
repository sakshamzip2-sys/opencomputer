# Prompt B — Affect Injection Plugin — Contract

**Date:** 2026-04-28
**Branch:** `feat/affect-work-abc`
**Status:** Implemented + tested. 10/10 affect-injection tests pass; broader sweep clean except 2 pre-existing failures unrelated to this work (see "Pre-existing failures" section).

---

## What this plugin does

`extensions/affect-injection/` registers a single `DynamicInjectionProvider` that contributes a structured `<user-state>` block to the system prompt every chat turn. It is read-only over `sessions.db`, the F4 graph (currently unused — reserved for future), and the global life-event registry.

Layered on top of Prompt A's cross-persona vibe groundwork: the per-session anchor (frozen base, "what state were they carrying in") is complemented by the per-turn block this provider emits ("what state are they in right now").

---

## `<user-state>` schema

```
<user-state>
vibe: <label>
recent_arc: <prev-vibe> -> <curr-vibe>
active_pattern: <pattern_id>
</user-state>
```

| Field | Source | Omitted when |
|-------|--------|--------------|
| `vibe` | Per-turn regex classification of last 1-2 user messages, falling back to `sessions.vibe` (set once at session start) when the per-turn classifier returns "calm" but the session-level value carries signal. | `primary_vibe` is `None`, `""`, or `"calm"`. |
| `recent_arc` | In-memory provider state: `_prev_turn_vibe[session_id]` snapshot vs current per-turn vibe. Updated every call. | No prior turn yet, OR prior == current (no transition). |
| `active_pattern` | `LifeEventRegistry.peek_most_recent_firing()` — non-destructive read; the chat surfacer's `drain_pending()` consumes the queue independently. | No firing in the queue, OR firing.surfacing != "hint" (silent firings stay out of chat). |

The block omits any line whose value is None/empty. If ALL three fields are absent, `collect()` returns `None` and the engine emits nothing.

Tag boundaries are fixed (`<user-state>` / `</user-state>`) so a downstream consumer (Prompt C, future tone-modulators, etc.) can extract the block with a simple substring search. Field order inside the tags is `vibe → recent_arc → active_pattern` and stable across calls.

---

## Cadence

Every chat turn — provider's `collect()` is called by `InjectionEngine.collect_all()` once per `run_conversation` step. Pure-function reads + an in-memory dict update; sub-millisecond. No DB writes.

`InjectionContext.turn_index` is honoured: when `> 0` and `< min_turns`, the provider returns `None` to stay silent during the opening turns. Default `min_turns = 2` (configurable via `AFFECT_INJECTION_MIN_TURNS` env var). `turn_index == 0` (the SDK's "caller did not thread the counter" sentinel) is treated as "always emit" per the contract on `InjectionContext.turn_index`.

---

## Provider metadata

- `provider_id`: `"affect-injection:v1"`.
- `priority`: `60` — runs after plan-mode (10) and yolo-mode (20), before generic user-defined modes (50+).
- `register_via`: `api.register_injection_provider(provider)` (the standard SDK surface).
- Async: yes, `collect()` is `async`. No `await` calls in the body — kept async for SDK compatibility.

---

## Read sources (no writes)

| What | API call | Failure mode |
|------|----------|--------------|
| Session vibe | `SessionDB.get_session_vibe(session_id)` returning `(vibe, vibe_updated)` | Try/except — degrades to `None` and the per-turn vibe still drives the block. |
| Per-turn vibe | `opencomputer.agent.vibe_classifier.classify_vibe(messages)` (regex, sub-ms) | Try/except — defaults to `"calm"` (no signal). |
| Active life-event | `LifeEventRegistry.peek_most_recent_firing()` (non-destructive) | Try/except — None. |

The SessionDB instance is constructed lazily on first `collect()` call and cached on `self._db`. Path comes from `api.session_db_path` at register time. When `db_path is None` (older SDK callers, tests not seeding a path), the `_read_session_vibe` returns `None` and the provider works in degraded mode.

---

## Cron-context skip

When `ctx.runtime.agent_context != "chat"`, `collect()` returns `None`. Mirrors `MemoryBridge.prefetch()` at `memory_bridge.py:233-234`: cron / flush / review batch turns shouldn't drag user-state framing into outputs not delivered to a user.

---

## Calm-gate vs Prompt A's calm-gate

These are independent:

- Prompt A's gate: the **cross-session anchor** (companion or neutral framing, in the FROZEN base prompt) is skipped when `prev_session_vibe == "calm"`.
- Prompt B's gate: the **per-turn `<user-state>` block** (in the per-turn deltas) is skipped when `primary_vibe == "calm"` AND no transition AND no active hint pattern.

Both gates apply the same heuristic — calm == no signal — but to different prompt layers and different vibe sources (cross-session prior vibe vs current-turn vibe).

---

## Constraints honoured

- **No SQL inline.** All reads go through `SessionDB` API methods.
- **No SQLite mutations.** `test_does_not_mutate_db` snapshots `sessions` + `vibe_log` rows before and after two `collect()` calls; both must match byte-for-byte.
- **No new SQLite columns.** Per-session arc state is in-memory only (`self._prev_turn_vibe: dict[str, str]`). Lost on process restart, which is fine — the arc detector falls through to "no arc" on the first turn after restart.
- **No LLM calls.** Pure regex (`vibe_classifier`) plus sqlite3 reads.
- **No new tool schemas.** `tool_names = []` in the manifest; `provider.tool_schemas()` is not implemented (this is an injection provider, not a tool provider).
- **No persona dependence.** The block surfaces uniformly; persona overlays are still rendered via the existing `_build_persona_overlay` path in Prompt A.

---

## Files

| Path | Purpose |
|------|---------|
| `extensions/affect-injection/plugin.json` | Manifest. `kind: "mixed"`, `enabled_by_default: false`, `tool_names: []`. Schema v2. |
| `extensions/affect-injection/plugin.py` | `register(api)`. Self-installs `extensions.affect_injection` namespace alias (mirrors `extensions/memory-honcho/plugin.py` because the plugin loader uses synthetic module names without parent packages). Builds provider via `affect_injection_provider_from_env(db_path=api.session_db_path)`, calls `api.register_injection_provider(provider)`. |
| `extensions/affect-injection/provider.py` | `AffectInjectionProvider(DynamicInjectionProvider)`. Implements `priority`, `provider_id`, `collect()`. Includes `affect_injection_provider_from_env()` factory that reads `AFFECT_INJECTION_MIN_TURNS`. |
| `tests/test_affect_injection.py` | 10 tests: provider id + priority + return-None gates (calm, cron, min_turns) + populated-block paths (transition, hint pattern, silent pattern omission) + integration through `InjectionEngine.compose()` + read-only DB invariant. |
| `tests/conftest.py` | Added `_register_affect_injection_alias()` so `from extensions.affect_injection.provider import AffectInjectionProvider` works under pytest. |

No `extensions/affect-injection/CONTRACT.md` was written separately — this audit-level contract IS the contract for downstream consumers (Prompt C). The plugin itself is small enough that its docstrings are the runtime documentation.

---

## Activation

`enabled_by_default: false`. To enable:

```bash
# Via env (recommended for first run)
export AFFECT_INJECTION_MIN_TURNS=2  # optional override
oc plugin enable affect-injection    # if a CLI exists; otherwise edit profile config
```

After enabling, every chat turn that produces non-calm signal will carry the `<user-state>` block.

---

## Pre-existing test failures (NOT caused by this work)

Two tests fail in the broader sweep on the affect-work branch AND on unmodified main:

- `tests/test_phase12b5_tool_names_field.py::test_bundled_plugin_manifests_have_accurate_tool_names` — drift detected in `coding-harness` because the optional `mss` dep is not installed in this venv, so `screenshot`, `extract_screen_text`, `list_app_usage`, `list_recent_files`, `read_clipboard_once` are declared in the manifest but not registered at runtime.
- `tests/test_phase5.py::test_doctor_run_returns_failure_count_zero_on_clean_env` — same root cause; doctor reports `mss missing` and `rapidocr_onnxruntime missing` failures.

Both verified to fail on `main` as well. They are environment issues (missing optional pip extras), not regressions. No fix attempted in this prompt — out of scope.

~~There is also one cosmetic warning emitted on every loader pass~~ — **FIXED 2026-04-28** in the same branch. `_validate_runtime_contract` at `loader.py:490-499` now includes `new_injection` in the `mixed`-kind drift check, so injection-only plugins (like this one) no longer trigger the spurious warning. Regression test: `tests/test_runtime_contract.py::test_mixed_kind_with_only_injection_provider_no_warning`.

**Also fixed in the same branch:** the two pre-existing test failures around `mss` / `rapidocr_onnxruntime` are resolved. `coding-harness/plugin.json` now declares the 5 introspection tools under a new `optional_tool_names` field; `test_phase12b5_tool_names_field` enforces `required ⊆ registered ⊆ required ∪ optional`; `doctor.py` downgrades missing-introspection-deps from `error` to `warning` (matches voice-mode's opt-in pattern). 4291 tests pass with 0 failures.

---

## Handoff to Prompt C

Prompt C consumes:

1. The `<user-state>` block lands in the per-turn delta path of the system prompt (specifically `injection_engine.compose()` at `loop.py:675-676`). It is NOT in the FROZEN base. Prompt C's `<user-tone>` block, by contrast, MUST be in the frozen base for prefix-cache stability — this is a separate channel.
2. The provider exposes no public state Prompt C can read; if a tone consumer wants to know "is the user currently frustrated", it should re-run `classify_vibe(last_user_messages)` itself (it's a sub-ms regex).
3. The `tone_preference` F4 node is NOT consumed by this plugin — it lives in Prompt C's surface area. There is no overlap.
4. Both contracts can coexist. The user prompt assembly looks like:

```
SYSTEM PROMPT (frozen base, prefix-cached):
  ... base.j2 contents ...
  ## Active persona  → companion/coding/etc overlay text
  ## RECENT LIFE EVENT (anchor for the companion)  → if companion + firing
  ## PREVIOUS-SESSION VIBE (anchor for the companion)  → if companion + non-calm prev
  ## Recent user state  → if non-companion + non-calm prev (Prompt A)
  <user-tone>...</user-tone>  → if F4 has tone_preference (Prompt C)

SYSTEM PROMPT (per-turn delta, recomputed each turn):
  ## Memory context  → ambient memory blocks (existing)
  <user-state>...</user-state>  → AffectInjectionProvider (this plugin)
  ## Relevant memory  → MemoryBridge.prefetch (existing)
```

Prompt C should ensure its `<user-tone>` injection runs in `prompt_builder.build()` (frozen base) NOT in any per-turn provider — confirmed by Prompt C's preconditions.

