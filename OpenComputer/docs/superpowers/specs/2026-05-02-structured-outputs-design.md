# Structured Outputs (Subsystem C)

**Date:** 2026-05-02
**Scope:** Provider-contract extension to enable schema-validated JSON responses, plus migration of one existing call site (`pattern_synthesizer`) as proof of value.
**Status:** Implementing in `feat/structured-outputs` (stacked on Subsystems A + B).

---

## 1. Problem

OpenComputer has several places where the agent calls a provider to produce structured-ish output:

- `evolution/pattern_synthesizer` — drafts a SKILL.md file (YAML frontmatter + markdown body), then validates with regex (must start with `---`, must have `name:` field, must be under cap).
- `extensions/skill-evolution/skill_extractor` — drafts text artifacts.
- Future skills, judges, classifiers built by users — currently must roll their own JSON-in-prompt + parse pattern.

Anthropic Doc 6 introduced `output_config.format` (JSON Schema) for guaranteed schema-compliant responses. OpenAI has had `response_format: {type: "json_schema"}` for a while. Both providers do server-side schema enforcement — schema-violating output cannot escape the provider.

OpenComputer doesn't expose this capability. Existing call sites either rely on regex validation (fragile) or don't validate at all. New use cases (LLM judges, classifiers, structured extraction) have to roll their own pattern each time.

## 2. Goals & non-goals

**Goals:**

- Add a `response_schema` parameter to the canonical provider contract (`BaseProvider.complete`).
- Wire Anthropic provider to translate `response_schema` → `output_config.format`.
- Wire OpenAI provider to translate `response_schema` → `response_format: {type: "json_schema"}`.
- Provide a `parse_structured()` helper that takes a Pydantic model + prompt and returns a parsed instance.
- Migrate `evolution/pattern_synthesizer` from regex-validated YAML-frontmatter parsing to schema-validated JSON output. Demonstrates real value: removes ~30 LOC of regex validators in favor of one Pydantic model.

**Non-goals (deferred):**

- Migrating other LLM-output call sites (`recall_synthesizer`, `title_generator`, `skill-evolution/skill_extractor`, `dreaming`, `evolution/reflect`). Each is a separate decision per call site.
- Native schema support for non-Anthropic/non-OpenAI providers (Kimi, Llama, Ollama). They get the JSON-in-prompt + post-hoc parse fallback; native upgrades land per-provider when those providers ship the capability.
- Streaming structured outputs (Anthropic supports this; OpenAI partial-JSON streaming has more nuance). Initial implementation is non-streaming only.
- Strict tool-use (`strict: true` on tool definitions). Separate concern even though Doc 6 mentions both.

## 3. Approach

### 3.1 Provider contract extension

Add to `plugin_sdk/provider_contract.py`:

```python
class JsonSchemaSpec(TypedDict, total=False):
    """Provider-agnostic JSON schema spec for structured outputs.

    The wire shape is intentionally minimal — providers translate to
    their native format (Anthropic ``output_config.format``, OpenAI
    ``response_format``). Models without native support fall back to
    JSON-in-prompt + post-hoc parse.
    """
    name: str           # for OpenAI's "name" field; ignored by Anthropic
    schema: dict        # the JSON schema (subset Anthropic + OpenAI both accept)
    description: str    # optional: one-liner for the schema (some providers surface this)
```

Extend `BaseProvider.complete` with an additive kwarg:

```python
async def complete(
    self,
    *,
    model: str,
    messages: list[Message],
    ...,
    response_schema: JsonSchemaSpec | None = None,   # NEW
) -> ProviderResponse: ...
```

Default `None` preserves existing behavior. All existing tests/stubs work without modification (kwarg is keyword-only with default).

### 3.2 Anthropic provider wiring

When `response_schema` is provided:

```python
if response_schema is not None:
    output_config = kwargs.get("output_config", {})
    output_config["format"] = {
        "type": "json_schema",
        "schema": response_schema["schema"],
    }
    kwargs["output_config"] = output_config
```

This composes cleanly with `output_config.effort` from Subsystem A — same parent dict, different keys.

### 3.3 OpenAI provider wiring

```python
if response_schema is not None:
    kwargs["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": response_schema.get("name", "response"),
            "schema": response_schema["schema"],
            "strict": True,
        },
    }
```

### 3.4 Helper module

New module `opencomputer/agent/structured.py`:

```python
async def parse_structured(
    *,
    response_model: type[BaseModel],
    messages: list[Message],
    provider: BaseProvider,
    model: str,
    system: str = "",
    max_tokens: int = 4096,
    name: str | None = None,
) -> BaseModel:
    """Call provider with schema-enforced response, return parsed Pydantic instance.

    Raises StructuredOutputError on:
    - Provider returns malformed JSON (shouldn't happen with native
      schema enforcement, but providers without native support may)
    - Pydantic validation fails on the parsed dict
    """
```

Implementation:
1. Convert Pydantic model to JSON schema via `response_model.model_json_schema()`
2. Call `provider.complete(..., response_schema={"name": name, "schema": schema})`
3. Parse `response.message.content` as JSON
4. Validate with `response_model.model_validate(parsed)`
5. Return instance

### 3.5 pattern_synthesizer migration

Define output model in `opencomputer/evolution/pattern_synthesizer.py`:

```python
class _SynthesizedSkill(BaseModel):
    """Schema for a synthesized SKILL.md draft."""
    name: str = Field(
        pattern=r"^[a-z][a-z0-9-]+$",
        min_length=3,
        max_length=64,
        description="Lowercase hyphenated slug",
    )
    description: str = Field(
        min_length=10,
        max_length=200,
        description="One-line skill description starting with 'Use when...'",
    )
    body: str = Field(
        min_length=50,
        description=(
            "Markdown body (no frontmatter — written separately). "
            "Includes # Title, ## When to use, ## Steps."
        ),
    )
```

Update prompt template to request structured JSON. The new template:
- Drops the "your output must start with `-`" rule
- Replaces SKILL.md format rules with field-level guidance (covered by the JSON schema)
- Keeps content rules: distinctive trigger tokens, no PII, no destructive commands

Update synthesizer flow:
1. Build prompt as before
2. Call `parse_structured()` with `_SynthesizedSkill`
3. Render SKILL.md text from the parsed instance (frontmatter + body)
4. Pass rendered text to `QuarantineWriter` via existing `QuarantinedSkill` dataclass

Keeps `QuarantinedSkill`/`QuarantineWriter` interface unchanged.

### 3.6 Fallback for providers without native support

Inside `parse_structured()`: if the provider's response stop_reason indicates the schema wasn't enforced (which we infer when the provider doesn't have native support), parse as JSON and raise `StructuredOutputError` on failure.

Detection: native schema support is per-provider. We don't add a contract method for this — providers that wire `response_schema` get native enforcement; providers that don't will pass through the kwarg as a no-op (via the additive default), and the LLM will produce whatever it produces. For non-supporting providers, callers should add explicit JSON instructions in the prompt as a backup.

This trade-off is acceptable: Anthropic + OpenAI cover ~95% of OpenComputer usage today. Other providers can ship native support per their own roadmap.

## 4. Components & file map

| File | Change |
|---|---|
| `plugin_sdk/provider_contract.py` | NEW: `JsonSchemaSpec` TypedDict. MODIFY: `BaseProvider.complete` signature gains `response_schema` kwarg. |
| `extensions/anthropic-provider/provider.py` | MODIFY: 3 call sites (`_do_complete`, `_do_stream_complete`, `stream_complete`) translate `response_schema` → `output_config.format`. |
| `extensions/openai-provider/provider.py` | MODIFY: similar translation to `response_format`. |
| `opencomputer/agent/structured.py` | NEW: `parse_structured()` helper + `StructuredOutputError`. |
| `opencomputer/evolution/pattern_synthesizer.py` | MODIFY: Pydantic model + use `parse_structured()`. Render SKILL.md from instance. |
| `opencomputer/evolution/prompts/synthesis_request.j2` | MODIFY: emit JSON structured fields. |
| `tests/test_provider_contract_response_schema.py` | NEW: kwarg passes through Anthropic + OpenAI providers. |
| `tests/test_structured_helper.py` | NEW: `parse_structured()` round-trip. |
| `tests/test_pattern_synthesizer_structured.py` | NEW or MODIFY: synthesizer uses schema-validated path. |

## 5. Generic-by-design checklist

- ✅ Capability lives in `plugin_sdk/provider_contract.py` — public, language-of-the-codebase, every provider can implement.
- ✅ Helper in `opencomputer/agent/structured.py` is provider-agnostic — takes any `BaseProvider`.
- ✅ Pydantic models for output schemas are user-defined — works for any classifier/judge/extraction use case.
- ✅ Future Kimi-Reasoner, DeepSeek-R1, Llama-thinking provider extensions add their own translation in their `provider.py` — no core changes.
- ✅ Providers without native support work as no-op (response_schema ignored) until they ship.

## 6. Testing strategy

- **Provider kwarg passthrough:** confirm `response_schema={"name": "X", "schema": {...}}` lands in Anthropic's `output_config.format` and OpenAI's `response_format`. Use captured-kwargs pattern (already used by `test_anthropic_provider_kwargs.py` from Subsystem A).
- **`parse_structured` round-trip:** stub provider returns valid JSON matching schema → assert returns parsed Pydantic instance. Stub returns invalid JSON → assert raises `StructuredOutputError`.
- **Pattern synthesizer schema migration:** existing tests for synthesizer (regex-validated path) get migrated to new schema-validated path. Stub provider returns valid `_SynthesizedSkill` JSON, synthesizer renders correct SKILL.md, `QuarantineWriter` writes the same way it did before.
- **BC:** existing `BaseProvider.complete` callers without `response_schema` kwarg work unchanged. Already verified by Subsystem A's test fix pattern (test stubs accept `**kwargs` or named optional).

## 7. Acceptance criteria

1. Full pytest suite green.
2. Ruff clean.
3. New tests: kwarg passthrough × 2 providers, helper round-trip × 2 cases, pattern synthesizer migration.
4. `pattern_synthesizer` produces valid SKILL.md content via schema-validated path; existing collision-detection / size-cap behavior preserved.
5. No regression in existing pattern_synthesizer integration tests.

## 8. Out of scope (follow-up PRs)

- Migrate other LLM-output call sites to schema-validated path
- Streaming structured outputs
- `strict: true` on tool definitions (different concern: tool input schemas, not output schemas)
- Native schema support for Kimi/Llama/Ollama providers
