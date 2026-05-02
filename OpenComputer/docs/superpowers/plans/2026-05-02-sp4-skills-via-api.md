# SP4 — Anthropic Skills-via-API opt-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire opt-in support for Anthropic's pre-built Skills-via-API (`pdf`/`pptx`/`xlsx`/`docx`) on the Anthropic provider, gated by a single runtime knob (`runtime.custom["anthropic_skills"]`) with env var fallback (`OPENCOMPUTER_ANTHROPIC_SKILLS`).

**Architecture:** Three new pure helpers (resolve / build container / augment kwargs) live next to the existing Anthropic provider. Provider's `complete()` and `stream_complete()` call the augmenter just before sending. Empty/unset → no kwargs change → today's behavior. Other providers (Bedrock, OpenAI) untouched.

**Tech Stack:** Python 3.12+, pytest, no new third-party deps.

**Spec:** [`docs/superpowers/specs/2026-05-02-sp4-skills-via-api-design.md`](../specs/2026-05-02-sp4-skills-via-api-design.md)

---

## Pre-flight

- [ ] **Step 0a: Verify worktree**

```bash
cd /private/tmp/oc-sp4-skills-via-api
git status
git branch --show-current
```

Expected: clean tree, on `feat/sp4-skills-via-api`.

- [ ] **Step 0b: Baseline pytest scope**

```bash
cd OpenComputer
pytest tests/ -k "anthropic or runtime_flag" --tb=short -q 2>&1 | tail -10
```

Expected: pass. Record count.

- [ ] **Step 0c: Baseline ruff**

```bash
ruff check extensions/anthropic-provider/ tests/
```

Expected: clean.

---

## Task 1: Pure helper functions + tests

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` (add 3 helpers + constants near top)
- Test: `tests/test_anthropic_skills_via_api.py` (NEW)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anthropic_skills_via_api.py`:

```python
"""Tests for Anthropic Skills-via-API helpers (SP4)."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_skills_via_api_provider", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime(custom: dict | None = None):
    """Build a SimpleNamespace mimicking RuntimeContext shape."""
    return SimpleNamespace(custom=custom or {})


# ─── _resolve_anthropic_skills ────────────────────────────────


def test_resolve_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == []
    assert module._resolve_anthropic_skills(None) == []


def test_resolve_reads_runtime_custom(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": ["pdf", "pptx"]})
    assert module._resolve_anthropic_skills(runtime) == ["pdf", "pptx"]


def test_resolve_reads_env_when_runtime_unset(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", "pdf,xlsx")
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == ["pdf", "xlsx"]
    # None runtime path also reads env
    assert module._resolve_anthropic_skills(None) == ["pdf", "xlsx"]


def test_resolve_runtime_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", "pdf,xlsx")
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": ["docx"]})
    assert module._resolve_anthropic_skills(runtime) == ["docx"]


def test_resolve_warns_on_bad_type(monkeypatch, caplog):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    module = _load_provider_module()
    runtime = _runtime({"anthropic_skills": "pdf"})  # str, should be list
    with caplog.at_level(logging.WARNING):
        result = module._resolve_anthropic_skills(runtime)
    assert result == []
    assert any("bad type" in r.message.lower() or "list" in r.message.lower()
               for r in caplog.records)


def test_resolve_strips_whitespace_and_drops_empty(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_SKILLS", " pdf , , xlsx ,  ")
    module = _load_provider_module()
    assert module._resolve_anthropic_skills(_runtime()) == ["pdf", "xlsx"]


# ─── _build_skills_container ──────────────────────────────────


def test_build_skills_container_shape():
    module = _load_provider_module()
    container = module._build_skills_container(["pdf", "pptx"])
    assert container == {
        "skills": [
            {"type": "anthropic", "skill_id": "pdf", "version": "latest"},
            {"type": "anthropic", "skill_id": "pptx", "version": "latest"},
        ]
    }


# ─── _augment_kwargs_for_skills ───────────────────────────────


def test_augment_noop_for_empty_skills():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7", "messages": []}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=[])
    assert out == {"model": "claude-opus-4-7", "messages": []}


def test_augment_adds_beta_headers():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7"}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    betas = out["extra_headers"]["anthropic-beta"].split(",")
    assert "code-execution-2025-08-25" in betas
    assert "skills-2025-10-02" in betas
    assert "files-api-2025-04-14" in betas


def test_augment_preserves_existing_betas():
    module = _load_provider_module()
    kwargs = {
        "model": "claude-opus-4-7",
        "extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"},
    }
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    betas = out["extra_headers"]["anthropic-beta"].split(",")
    assert "prompt-caching-2024-07-31" in betas
    assert "skills-2025-10-02" in betas


def test_augment_adds_container():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7"}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf", "xlsx"])
    assert out["container"]["skills"][0]["skill_id"] == "pdf"
    assert out["container"]["skills"][1]["skill_id"] == "xlsx"


def test_augment_adds_code_execution_tool():
    module = _load_provider_module()
    kwargs = {"model": "claude-opus-4-7", "tools": []}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    tool_types = [t.get("type") for t in out["tools"]]
    assert "code_execution_20250825" in tool_types


def test_augment_no_duplicate_tool_when_already_present():
    module = _load_provider_module()
    existing_tool = {"type": "code_execution_20250825", "name": "code_execution"}
    kwargs = {"model": "claude-opus-4-7", "tools": [existing_tool]}
    out = module._augment_kwargs_for_skills(kwargs=kwargs, skill_ids=["pdf"])
    code_exec_count = sum(
        1 for t in out["tools"] if t.get("type") == "code_execution_20250825"
    )
    assert code_exec_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_skills_via_api.py -v
```

Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Add the constants + 3 helpers to provider.py**

In `extensions/anthropic-provider/provider.py`, near the top of the module (after existing imports, before the existing helper functions), add:

```python
import os

ANTHROPIC_SKILLS_BETA_HEADERS = (
    "code-execution-2025-08-25",
    "skills-2025-10-02",
    "files-api-2025-04-14",
)

CODE_EXECUTION_TOOL = {
    "type": "code_execution_20250825",
    "name": "code_execution",
}


def _resolve_anthropic_skills(runtime) -> list[str]:
    """Get the list of Anthropic skill IDs to enable for this call.

    Resolution order:
    1. runtime.custom["anthropic_skills"] (explicit programmatic)
    2. OPENCOMPUTER_ANTHROPIC_SKILLS env var (comma-separated)
    3. [] (no skills)

    Bad input (non-list, non-strings) is logged and ignored.
    """
    if runtime is not None:
        explicit = (getattr(runtime, "custom", {}) or {}).get("anthropic_skills")
        if explicit is not None:
            if isinstance(explicit, list) and all(isinstance(s, str) for s in explicit):
                return [s for s in explicit if s.strip()]
            _log.warning(
                "anthropic_skills runtime flag has bad type %r; expected list[str]",
                type(explicit).__name__,
            )
            return []
    env = os.environ.get("OPENCOMPUTER_ANTHROPIC_SKILLS", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return []


def _build_skills_container(skill_ids: list[str]) -> dict:
    """Build the container.skills array per Anthropic Skills-via-API spec."""
    return {
        "skills": [
            {"type": "anthropic", "skill_id": sid, "version": "latest"}
            for sid in skill_ids
        ]
    }


def _augment_kwargs_for_skills(
    *,
    kwargs: dict,
    skill_ids: list[str],
) -> dict:
    """Mutate kwargs to enable Anthropic Skills-via-API.

    - Adds the three required beta headers (preserving any existing ones).
    - Adds container.skills.
    - Appends code_execution_20250825 to tools (avoids duplicates).

    Returns the same dict (mutated) for convenience. Empty/no-op when
    skill_ids is empty.
    """
    if not skill_ids:
        return kwargs

    # Beta headers — preserve any existing comma-separated betas
    extra = dict(kwargs.get("extra_headers") or {})
    existing_betas = [
        b.strip() for b in extra.get("anthropic-beta", "").split(",") if b.strip()
    ]
    for beta in ANTHROPIC_SKILLS_BETA_HEADERS:
        if beta not in existing_betas:
            existing_betas.append(beta)
    if existing_betas:
        extra["anthropic-beta"] = ",".join(existing_betas)
    kwargs["extra_headers"] = extra

    # container.skills
    kwargs["container"] = _build_skills_container(skill_ids)

    # code_execution tool (required for skills to run)
    tools = list(kwargs.get("tools") or [])
    if not any(t.get("type") == "code_execution_20250825" for t in tools):
        tools.append(CODE_EXECUTION_TOOL)
    kwargs["tools"] = tools

    return kwargs
```

(Note: `_log` is the existing module-level logger used elsewhere in `provider.py`. If the existing logger variable is named differently — `log`, `logger`, etc. — use that name. Verify with `grep -n "logging.getLogger" extensions/anthropic-provider/provider.py`.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_skills_via_api.py -v
```

Expected: all 13 PASS.

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp4-skills-via-api
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_skills_via_api.py
git commit -m "feat(anthropic-provider): Skills-via-API helpers (resolve / build / augment)"
```

---

## Task 2: Wire helpers into Anthropic provider's complete()/stream_complete()

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` (call augmenter at message-send sites)

- [ ] **Step 1: Find the message-send call sites**

```bash
grep -n "client.messages.create\|client.messages.stream" extensions/anthropic-provider/provider.py
```

Per earlier survey, the matches are at lines ~624, ~651, ~667, ~866, ~1053. Read each in context to understand which is `complete()`, which is `stream_complete()`, which is `complete_vision()`, etc.

- [ ] **Step 2: For each callsite, find the runtime parameter being passed in**

The `runtime` parameter flows from `complete(messages, ..., runtime=...)` down through helpers. Look for where `runtime` is in scope at each callsite. If `runtime` isn't in the local scope at a callsite, trace the call chain back.

- [ ] **Step 3: Write a regression test verifying the wire-up**

Add to `tests/test_anthropic_skills_via_api.py`:

```python
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock


def test_provider_complete_calls_augment_when_skills_set(monkeypatch):
    """Integration: provider.complete() must augment kwargs when anthropic_skills set.

    Patches the Anthropic SDK client to capture the kwargs and asserts
    container.skills was injected.
    """
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_SKILLS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    module = _load_provider_module()

    captured_kwargs = {}

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text="ok")]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_response.model = "claude-opus-4-7"
    fake_response.id = "msg_test"

    async def fake_create(**kw):
        captured_kwargs.update(kw)
        return fake_response

    # Find the provider class and instantiate
    Provider = module.AnthropicProvider  # or whatever the public class is
    provider = Provider(model="claude-opus-4-7")

    # Patch the SDK client
    fake_client = MagicMock()
    fake_client.messages.create = fake_create
    monkeypatch.setattr(provider, "_client", fake_client, raising=False)

    runtime = _runtime({"anthropic_skills": ["pdf"]})

    # Call provider.complete with whatever signature it has
    from plugin_sdk.core import Message
    messages = [Message(role="user", content="hi")]

    try:
        asyncio.run(provider.complete(messages=messages, runtime=runtime, max_tokens=10))
    except Exception:
        # Some downstream cost-tracking code may fail with the mock
        # response; we only care about the captured kwargs.
        pass

    assert "container" in captured_kwargs
    assert captured_kwargs["container"]["skills"][0]["skill_id"] == "pdf"
    tool_types = [t.get("type") for t in captured_kwargs.get("tools") or []]
    assert "code_execution_20250825" in tool_types
```

(This test is approximate — adapt to the actual provider class name, instantiation pattern, and `complete()` signature found in Step 1. The assertion on `captured_kwargs` is the load-bearing part.)

- [ ] **Step 4: Run the integration test to verify it fails**

```bash
pytest tests/test_anthropic_skills_via_api.py::test_provider_complete_calls_augment_when_skills_set -v
```

Expected: FAIL — augmenter not yet wired into `complete()`.

- [ ] **Step 5: Wire the augmenter into complete()**

For each callsite found in Step 1 that is part of `complete()` or `stream_complete()` (not `complete_vision()` — that's its own path):

Find the line `kwargs = {...}` or equivalent that builds the request kwargs, and AFTER that line (but BEFORE `client.messages.create(**kwargs)`), add:

```python
kwargs = _augment_kwargs_for_skills(
    kwargs=kwargs,
    skill_ids=_resolve_anthropic_skills(runtime),
)
```

If the kwargs dict is built incrementally (multiple lines of `kwargs["x"] = y`), do this AFTER all the building is done — just before the final API call.

If `runtime` isn't in scope at the callsite, plumb it down through the helper functions that produce kwargs. Or — pass `runtime` directly to `_augment_kwargs_for_skills` if that's cleaner. Adapt to whatever the actual code structure supports.

**For `complete_vision()`:** decide whether to augment or not. Vision + skills is conceptually orthogonal — a user might want both ("analyze this image AND make a PowerPoint"). Wiring it in is consistent. Cost is one extra function call. Do wire it.

- [ ] **Step 6: Run the integration test + scoped regression**

```bash
pytest tests/test_anthropic_skills_via_api.py -v --tb=short
pytest tests/ -k "anthropic" --tb=line -q | tail -10
```

Expected: integration test PASSES; full anthropic regression sweep PASSES.

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/oc-sp4-skills-via-api
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_skills_via_api.py
git commit -m "feat(anthropic-provider): wire Skills-via-API augmenter into complete()/stream_complete()"
```

---

## Task 3: Documentation

**Files:**
- Create: `docs/providers/anthropic-skills-via-api.md` (NEW)

- [ ] **Step 1: Create parent dir + write the doc**

```bash
mkdir -p /private/tmp/oc-sp4-skills-via-api/OpenComputer/docs/providers
```

Create `docs/providers/anthropic-skills-via-api.md`:

```markdown
# Anthropic Skills-via-API (opt-in)

OpenComputer supports invoking Anthropic's pre-built skills (`pdf`,
`pptx`, `xlsx`, `docx`) running in Anthropic's code-execution container.
This lets you generate documents (PowerPoints, spreadsheets, Word files,
PDFs) without bundling local Python dependencies like python-pptx /
openpyxl / python-docx.

**This is OFF by default** because it adds a cloud round-trip and
server-side execution cost. Enable per call when you actually need it.

## Enable

### Programmatic (per-session or per-call)

```python
from opencomputer.agent.runtime import RuntimeContext

runtime = RuntimeContext(custom={"anthropic_skills": ["pdf", "pptx"]})
# Pass runtime to your agent loop / provider.complete() call
```

### Environment variable

```bash
export OPENCOMPUTER_ANTHROPIC_SKILLS=pdf,pptx,xlsx,docx
opencomputer chat
```

If both are set, the runtime flag takes precedence.

## What gets injected per request

When `anthropic_skills` is non-empty, the Anthropic provider auto-adds:

1. **Beta headers**: `code-execution-2025-08-25`, `skills-2025-10-02`,
   `files-api-2025-04-14`.
2. **`container.skills`**: array listing each enabled skill with
   `type=anthropic, version=latest`.
3. **`code_execution_20250825` tool**: required for skills to actually
   run; auto-appended to your tools list (no duplicates).

Empty or unset → today's behavior (no kwargs change).

## Available Anthropic-managed skills

| Skill ID | What it does |
|---|---|
| `pdf` | Generate or modify PDF files (forms, reports). |
| `pptx` | Create or edit PowerPoint presentations. |
| `xlsx` | Create or edit Excel spreadsheets, charts, pivot tables. |
| `docx` | Create or edit Word documents. |

(Per Anthropic's Skills-via-API guide. List may grow.)

## Trade-offs

| Aspect | Detail |
|---|---|
| **Latency** | Each call now routes through Anthropic's container before responding. Adds ~1-3s per turn even for simple text replies. |
| **Cost** | Server-side execution is metered. Generating a 5-slide PowerPoint can be substantially more expensive than a plain text reply. |
| **ZDR** | Skills-via-API is **NOT** ZDR-eligible. Files written by skills are retained per Anthropic's standard policy. |
| **Provider lock-in** | Anthropic-only. Bedrock / OpenAI / others ignore the flag. |

## When to use

✅ User asks "create a PowerPoint summarizing this conversation"
✅ User asks "build a spreadsheet of my expenses from this CSV"
✅ User asks "fill out this PDF form"

## When NOT to use

❌ Any task you can do with OC's local tools (Bash, Read/Write/Edit,
   WebSearch). Local is faster, free, and ZDR-eligible.
❌ Multi-turn coding sessions. The skills container's compute cost
   dwarfs Bash's.
❌ Default-on. The user's local-execution agent should stay local
   unless they opt into the cloud capability.

## Implementation references

- Spec: `OpenComputer/docs/superpowers/specs/2026-05-02-sp4-skills-via-api-design.md`
- Plan: `OpenComputer/docs/superpowers/plans/2026-05-02-sp4-skills-via-api.md`
- Helpers: `extensions/anthropic-provider/provider.py::_resolve_anthropic_skills`,
  `_build_skills_container`, `_augment_kwargs_for_skills`
- Anthropic Skills-via-API guide: https://docs.claude.com/en/build-with-claude/skills-guide
- Code execution tool docs: https://docs.claude.com/en/agents-and-tools/tool-use/code-execution-tool
```

- [ ] **Step 2: Commit**

```bash
cd /private/tmp/oc-sp4-skills-via-api
git add OpenComputer/docs/providers/anthropic-skills-via-api.md
git commit -m "docs(providers): Anthropic Skills-via-API opt-in guide + trade-offs"
```

---

## Task 4: Final verification + push + PR

- [ ] **Step 1: Run FULL pytest**

```bash
cd /private/tmp/oc-sp4-skills-via-api/OpenComputer
pytest tests/ --tb=line -q --ignore=tests/test_voice 2>&1 | tail -10
```

Expected: all pass. Compare to baseline.

- [ ] **Step 2: Run FULL ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean.

- [ ] **Step 3: Push the branch**

```bash
cd /private/tmp/oc-sp4-skills-via-api
git push -u origin feat/sp4-skills-via-api
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --title "feat(providers): Anthropic Skills-via-API opt-in (SP4)" --body "$(cat <<'EOF'
## Summary

SP4 of the Anthropic-API-parity scope (final). Spec: \`docs/superpowers/specs/2026-05-02-sp4-skills-via-api-design.md\`. Plan: \`docs/superpowers/plans/2026-05-02-sp4-skills-via-api.md\`.

- **Single runtime knob**: \`runtime.custom["anthropic_skills"]\` (list of skill IDs) — env var fallback \`OPENCOMPUTER_ANTHROPIC_SKILLS\`.
- **Auto-injection** when set: beta headers (code-execution-2025-08-25, skills-2025-10-02, files-api-2025-04-14), \`container.skills\` array, \`code_execution_20250825\` tool.
- **Anthropic provider only.** Bedrock + OpenAI ignore the flag (no equivalent feature).
- **Default OFF.** No automatic inference; explicit opt-in.

### Test plan
- [x] \`pytest tests/test_anthropic_skills_via_api.py\` — 13 unit + 1 integration test
- [x] Anthropic regression sweep — green
- [x] Full pytest suite — green
- [x] \`ruff check\` — clean

### Honest framing
SP4's value is narrow: it enables generating PowerPoints / Excel files / Word docs / PDFs via Anthropic's hosted skills container, without bundling local libraries. For everything else, OC's local tools (Bash, Read/Write/Edit, WebSearch) remain the recommended path — they're faster, free, and ZDR-eligible. SP4 is OFF by default and explicitly opt-in for that reason. See \`docs/providers/anthropic-skills-via-api.md\` for trade-offs.

### Out of scope
- Server-side \`web_search\` tool (OC has multi-backend equivalent)
- Standalone \`code_execution\` flag (bundled with skills; required for them to run)
- CLI command (\`oc skills enable anthropic:pdf\`) — env var is sufficient
- Custom user-uploaded Skills-API workflow (consume only; SP3 covered Files API for outputs)
- Cost guards on server-side skill execution (separate follow-up if usage emerges)
- Bedrock / OpenAI Skills-API (no API equivalent)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Report PR URL**

---

## Self-Review

**Spec coverage:**
| Spec section | Task |
|---|---|
| §5.1 Runtime flag definition | Task 1 (resolver helper) |
| §5.2 Provider integration | Task 1 (helpers) + Task 2 (wire-up) |
| §5.3 Behavior matrix | Implicit: Bedrock/OpenAI providers untouched |
| §5.4 Errors | Task 1 (bad-type + empty-list tests) |
| §5.5 Tests | Tasks 1, 2 (TDD) |
| §5.6 Documentation | Task 3 |

**Placeholder scan:** No "TBD" / "fill in later" outside conditional plumbing in Task 2 Step 5 (which is correctly conditional on the actual call structure).

**Type consistency:**
- `_resolve_anthropic_skills(runtime) -> list[str]` consistent.
- `_build_skills_container(skill_ids: list[str]) -> dict` consistent.
- `_augment_kwargs_for_skills(*, kwargs: dict, skill_ids: list[str]) -> dict` consistent.
- `ANTHROPIC_SKILLS_BETA_HEADERS` (tuple) and `CODE_EXECUTION_TOOL` (dict) constant names consistent across tasks.
