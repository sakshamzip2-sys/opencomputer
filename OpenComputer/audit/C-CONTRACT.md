# Prompt C — Tone preference consumption — Contract

**Date:** 2026-04-28
**Branch:** `feat/affect-work-abc`
**Status:** Implemented + tested. 7/7 user-tone tests pass; 59/59 across all three prompts (A/B/C tests combined); 177/177 in the broader prompt_builder + user_model + persona + vibe + affect scope.

---

## What changed

### 1. New `PromptBuilder.build_user_tone()` method

**Location:** `opencomputer/agent/prompt_builder.py` — between `build_user_facts()` and `build_with_memory()`.

**Signature:**

```python
def build_user_tone(
    self,
    *,
    store: UserModelStore | None = None,
) -> str:
    """Return the bare ``tone_preference`` value, or "" if not set."""
```

**Read path:**

1. Call `UserModelStore.list_nodes(kinds=("preference",), limit=100)`.
2. Filter to nodes whose `value` starts with `tone_preference:`.
3. Sort by `(-confidence, -last_seen_at)` so the top entry is highest-confidence + most-recent.
4. Strip the `tone_preference:` prefix and return the bare value (no leading/trailing whitespace).
5. Return `""` when no matching node exists.

The `tone_preference:` prefix convention is set at write time in `opencomputer/profile_bootstrap/persistence.py:117-121`:

```python
s.upsert_node(
    kind=kind,
    value=f"{question_key}: {a}",
    confidence=1.0,
)
```

### 2. PromptContext + build() + build_with_memory() additions

A new `user_tone: str = ""` field on `PromptContext` (`prompt_builder.py:166-174`) with a docstring explaining the FROZEN-base intent.

`PromptBuilder.build()` and `PromptBuilder.build_with_memory()` both accept `user_tone: str = ""` and thread it through the Jinja2 template context. Default is `""` so every existing caller (tests, fixtures, direct invocations) continues to work without modification.

### 3. base.j2 block

Inserted between `{% if user_facts %}` and `{% if persona_overlay %}` (`prompts/base.j2`):

```jinja
{% if user_tone -%}
<user-tone>
{{ user_tone }}
</user-tone>

The user stated this as their tone preference during onboarding. Honour it as the default register — it overrides any persona-default tone. Match this preference unless the current task makes a different register clearly more useful (e.g. a bug is forcing precision the user didn't ask for).

{% endif %}
```

The block lives in the FROZEN base (the `build()` template path) so the Anthropic prefix cache stays hot. Verified by `test_build_includes_user_tone_block_when_present` — the block appears in the output of `pb.build(user_tone=...)` directly, no per-turn delta path involved.

### 4. Loop wire-up

`opencomputer/agent/loop.py:621-637` (added after persona_overlay, before `build_with_memory` call): a try/except that calls `self.prompt_builder.build_user_tone()` and degrades to `""` on any failure, then passes `user_tone=user_tone` into `build_with_memory`. Same lane as `user_facts` / `workspace_context` / `persona_overlay` so it runs once per session and lands on the frozen base prompt.

---

## Read path summary

```
sessions.db.user_model.graph.sqlite (F4)
    ↓ list_nodes(kinds=("preference",), limit=100)
filter on value.startswith("tone_preference:")
    ↓
sort by (-confidence, -last_seen_at)
    ↓ pick first
strip "tone_preference:" prefix
    ↓ return bare value (or "" if none)
PromptBuilder.build_user_tone() → str
    ↓ passed as user_tone= kwarg
PromptBuilder.build() / build_with_memory()
    ↓ rendered into base.j2 via Jinja2
<user-tone>{value}</user-tone> in FROZEN base prompt
```

---

## Precedence rule (and a Gotcha)

### What the prompt asked for

"User-stated tone_preference takes precedence over persona preferred_tone in the persona overlay rendering."

### What I implemented

The `<user-tone>` block in base.j2 carries instruction text that says: *"Honour it as the default register — it overrides any persona-default tone."* This makes the precedence explicit to the LLM through prompt instruction, which is the only enforcement surface available without touching code outside Prompt C's scope.

### ~~Gotcha — persona `preferred_tone` is currently inert~~ — FIXED 2026-04-28

**Status:** Wired. The persona's `preferred_tone` is now consumed by the prompt assembly with code-level precedence enforcement.

**Implementation:**
- `_build_persona_overlay` at `loop.py` sets `self._active_persona_preferred_tone = persona.get("preferred_tone", "")` alongside the existing `self._active_persona_id`.
- `loop.py` passes it through to `prompt_builder.build_with_memory(persona_preferred_tone=...)`.
- `PromptContext` carries `persona_preferred_tone: str = ""`.
- `base.j2` renders a `<persona-tone>` block with the precedence rule:
  ```jinja
  {% if user_tone -%}
  <user-tone>...</user-tone>
  {% elif persona_preferred_tone -%}
  <persona-tone>...</persona-tone>
  {% endif %}
  ```
  Code-level enforcement: when `user_tone` is set, the `<persona-tone>` block is suppressed (the `{% elif %}` is unreachable). The persona's `system_prompt_overlay` continues to render (other persona aspects like response format are unaffected) — only the tone field is overridden.

**Tests** (in `tests/test_user_tone_injection.py`):
- `test_persona_preferred_tone_renders_when_user_tone_absent`
- `test_user_tone_overrides_persona_preferred_tone_in_code`
- `test_persona_preferred_tone_omitted_when_persona_has_none`

All pass. The "user wins" rule is now enforced in code, not just prompt instruction.

### What this means for callers

- The user's stated tone is rendered as `<user-tone>VALUE</user-tone>` in the frozen base.
- The persona overlay text (whatever the YAML's `system_prompt_overlay` says) renders below it.
- The LLM is instructed to prefer the user's stated tone over persona-default tone via the block's accompanying text.
- Code-level enforcement of the precedence is not introduced because there is nothing in code to enforce against. Future work tracking item.

---

## Constraints honoured

- **Block goes in the FROZEN base prompt (Pass 1).** Verified — invoked from `PromptBuilder.build()` via Jinja2 template context, NOT from a per-turn DynamicInjectionProvider.
- **Block stays small.** The injected value is the user's literal answer (typically 5-15 words). The accompanying instruction text is fixed at one paragraph.
- **No new bootstrap question.** The existing question 3 of `quick_interview.py` is consumed unchanged.
- **No F4 schema changes.** `kind="preference"` and the `tone_preference:` prefix convention already exist.
- **No new CLI command.** Prompt C explicitly excluded one. `oc user-model` already lets the user inspect / edit.
- **No interaction with Prompt B's `<user-state>` block.** Tone is in the frozen base; user-state is a per-turn injection. They render in different prompt sections.

---

## Files touched

| File | Change |
|------|--------|
| `opencomputer/agent/prompt_builder.py` | Added `user_tone: str` to PromptContext, `user_tone` kwarg to `build()` and `build_with_memory()` (threaded into Jinja2 ctx), and the new `build_user_tone()` method (43 lines, including docstring). |
| `opencomputer/agent/prompts/base.j2` | Inserted the `<user-tone>` block between `{% if user_facts %}` and `{% if persona_overlay %}`. |
| `opencomputer/agent/loop.py` | Added a 17-line block after persona_overlay computation that calls `build_user_tone()` (with try/except degrade to `""`) and threads the result into `build_with_memory()`. |
| `tests/test_user_tone_injection.py` | New file — 7 tests: empty-graph case, prefix-strip, non-tone-preference filter, highest-confidence pick, most-recent at equal confidence, integration through `build()`, omission when no tone is set. |

No new CLI sub-app, no new manifest, no new SQLite schema. Net diff is ~80 lines of code + ~120 lines of tests.

---

## Tests

`tests/test_user_tone_injection.py` (7 tests, all passing):

1. `test_user_tone_empty_when_no_node` — empty graph → `""`.
2. `test_user_tone_extracts_value_stripping_prefix` — bare value returned, prefix stripped.
3. `test_user_tone_skips_non_tone_preferences` — `do_not:` and `favourite_editor:` nodes don't match.
4. `test_user_tone_picks_highest_confidence_when_multiple` — confidence 1.0 beats 0.6.
5. `test_user_tone_picks_most_recent_at_equal_confidence` — most-recent upsert wins on ties.
6. `test_build_includes_user_tone_block_when_present` — integration: `<user-tone>` tag in rendered output, `tone_preference:` prefix is NOT in the output.
7. `test_build_omits_user_tone_block_when_empty` — block absent when `user_tone == ""`.

Broader sweep (177 tests across prompt_builder + user_model + persona + vibe + affect + learning_moments scopes): all pass.

---

## ~~Pre-existing failures~~ — FIXED 2026-04-28

Both originally-failing tests now pass. Root cause was that `coding-harness` declared 5 optional-dep-gated introspection tools as required, and `doctor.py` classified missing-introspection-deps as `error`. Fix:

- `plugin_sdk/core.py` + `manifest_validator.py`: new `optional_tool_names` field on `PluginManifest` for tools gated on optional pip extras.
- `extensions/coding-harness/plugin.json`: the 5 introspection tools moved from `tool_names` to `optional_tool_names`.
- `opencomputer/plugins/discovery.py` + `demand_tracker.py`: pass through and query both lists.
- `opencomputer/doctor.py`: missing `mss` / `rapidocr_onnxruntime` now `level="warning"` (matches the voice-mode opt-in pattern).
- `tests/test_phase12b5_tool_names_field.py`: invariant updated to `required ⊆ registered ⊆ required ∪ optional`.
- `tests/test_doctor_introspection_checks.py::test_introspection_deps_flags_missing`: assertion updated to expect `warning`, not `error`.

Final verification: 4291 passed, 0 failed (full suite minus tests that require `mss` import to even load — those are skipped at collection).

---

## Risk assessment

The `<user-tone>` block lands inside the prefix-cached system prompt. If the F4 graph changes mid-session (the user re-runs bootstrap, edits a node), the change does NOT affect the current session — the snapshot is built once at `loop.py:634` and reused. The next session will pick up the new value. This is intentional (matches the existing `user_facts` / `workspace_context` / `persona_overlay` pattern) and prevents prefix-cache invalidation, but worth flagging: a user who edits their tone preference must restart their session for it to take effect.

The `tone_preference` field can be arbitrary user-typed text (not enumerated). The block renders the value verbatim. A pathological 10K-character answer would expand the block, but the F4 layer caps `value` at the SQLite TEXT default (no hard limit configured) so this is more theoretical than practical. If a future tightening is needed, cap at `~200` chars in `build_user_tone()` — easy follow-up.

---

## Handoff to whoever runs the next thing

The original three-prompt plan ends here. After all three:

1. The vibe classifier runs on every persona (Prompt A — was largely already done; this prompt added the cross-session anchor side).
2. The `<user-state>` injection surface emits per-turn signal (Prompt B — new plugin at `extensions/affect-injection/`).
3. The bootstrap-captured tone preference is consumed (Prompt C — this contract).

Recommended next steps (in priority order, NOT in scope here):

1. **Wire `preferred_tone` from persona YAMLs into the prompt** (small): closes the gap noted in the Gotcha section. Without it, the "user tone overrides persona tone" rule is enforced purely via prompt instruction.
2. **Enable the `affect-injection` plugin in the default profile** by flipping `enabled_by_default: true` in the manifest, after a day of dogfooding.
3. **Address Risk Register items RR-3 + RR-7 from `06-privacy.md`** before any non-self user is onboarded. They are unrelated to this work but they are real privacy / operational hazards.
4. **Improve the loader's `mixed`-kind drift detection** to count `register_injection_provider` calls (cosmetic warning today; one-line fix in `opencomputer/plugins/loader.py:490-498`).
5. **Add `oc memory dream-now` cron auto-fire** if you want consolidations to happen unattended. Currently they only fire on the explicit CLI command.

