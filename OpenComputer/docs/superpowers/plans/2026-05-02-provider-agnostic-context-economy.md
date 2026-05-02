# Provider-Agnostic Context Economy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Anthropic thinking-block resend bug, surface cache-cost telemetry uniformly, apply caching micro-optimizations behind a `ProviderCapabilities` abstraction so the next provider added inherits these features by declaration.

**Architecture:** New `ProviderCapabilities` dataclass on `BaseProvider`. Add `reasoning_replay_blocks` field to `Message` and `ProviderResponse` for verbatim provider-side reasoning replay (Anthropic thinking with signatures, future use). Patch `_to_anthropic_messages` and `_parse_response` to capture and reconstruct thinking blocks. Capability-aware filters in `prompt_caching.py`. Cache-cost surfacing in `StepOutcome` and `/usage`.

**Tech stack:** Python 3.12+, dataclasses, pytest, ruff, the existing `anthropic`/`openai` SDKs.

**Spec:** `docs/superpowers/specs/2026-05-02-provider-agnostic-context-economy-design.md`

**Branch:** `feat/provider-context-economy` off `main` (in a git worktree at `/Users/saksham/Vscode/claude-worktrees/context-economy`).

---

## Pre-flight: worktree setup

The user has a hard rule: never let two Claude sessions share one working tree on a branch. Another session is active in this repo right now.

- [ ] **Step 1: Create the worktree off main**

```bash
cd /Users/saksham/Vscode/claude
git fetch origin main
git worktree add -b feat/provider-context-economy /Users/saksham/Vscode/claude-worktrees/context-economy origin/main
cd /Users/saksham/Vscode/claude-worktrees/context-economy
```

- [ ] **Step 2: Confirm clean baseline**

```bash
cd /Users/saksham/Vscode/claude-worktrees/context-economy/OpenComputer
source .venv/bin/activate || python3.13 -m venv .venv && source .venv/bin/activate
pip install -e .[dev] -q
pytest tests/test_phase6a.py -q  # SDK-boundary smoke
ruff check plugin_sdk/ opencomputer/agent/ extensions/anthropic-provider/ -q
```

Expected: SDK boundary test passes, ruff clean.

---

## Task 1: Add `CacheTokens` + `ProviderCapabilities` to plugin SDK

**Files:**
- Modify: `OpenComputer/plugin_sdk/provider_contract.py`
- Modify: `OpenComputer/plugin_sdk/__init__.py`
- Test: `OpenComputer/tests/test_provider_capabilities.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_capabilities.py
"""Capability struct defaults + per-provider declarations."""
from plugin_sdk import ProviderCapabilities, CacheTokens


def test_capabilities_defaults_are_safe():
    caps = ProviderCapabilities()
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
    assert caps.supports_long_ttl is False
    # Defaults must yield zero cache tokens for any synthetic usage object.
    assert caps.extracts_cache_tokens(object()) == CacheTokens(read=0, write=0)
    # Default min-cache-tokens is 0 (no filter).
    assert caps.min_cache_tokens("any-model") == 0


def test_cache_tokens_default_zero():
    ct = CacheTokens()
    assert ct.read == 0
    assert ct.write == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_provider_capabilities.py -q
```
Expected: ImportError — `ProviderCapabilities` and `CacheTokens` don't exist yet.

- [ ] **Step 3: Add the dataclasses to plugin_sdk/provider_contract.py**

Insert after the existing `Usage` class (around line 31):

```python
from collections.abc import Callable as _Callable


@dataclass(frozen=True, slots=True)
class CacheTokens:
    """Provider-agnostic cache token counts extracted from a usage payload."""

    read: int = 0
    write: int = 0


def _default_extract_cache_tokens(usage: Any) -> "CacheTokens":  # noqa: ARG001
    """Conservative default — providers without cache visibility return zeros."""
    return CacheTokens()


def _default_min_cache_tokens(model: str) -> int:  # noqa: ARG001
    """No filtering by default."""
    return 0


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider supports for the agent loop's context-economy decisions.

    All fields default to conservative "off" values so a provider that does
    nothing inherits today's behaviour.

    * ``requires_reasoning_resend_in_tool_cycle`` — set True if the provider
      requires the assistant message that originally produced a tool_use to
      include the corresponding reasoning block (with signature) when the
      tool_result is sent back. Anthropic extended thinking requires this;
      OpenAI Chat Completions does not.
    * ``reasoning_block_kind`` — opaque tag the provider uses to distinguish
      its reasoning replay shape (e.g. ``"anthropic_thinking"``).
    * ``extracts_cache_tokens`` — callable that maps the provider's usage
      payload to ``CacheTokens``. Default returns zeros.
    * ``min_cache_tokens`` — minimum block size (in tokens) for which a
      cache_control marker is worth placing. Provider-aware; receives the
      model name. Default returns 0 (no filter).
    * ``supports_long_ttl`` — True if the provider exposes a 1-hour cache
      TTL knob (Anthropic only today).
    """

    requires_reasoning_resend_in_tool_cycle: bool = False
    reasoning_block_kind: Literal["anthropic_thinking", "openai_reasoning", None] = None
    extracts_cache_tokens: _Callable[[Any], "CacheTokens"] = _default_extract_cache_tokens
    min_cache_tokens: _Callable[[str], int] = _default_min_cache_tokens
    supports_long_ttl: bool = False
```

Update `__all__` at the bottom:

```python
__all__ = [
    "BaseProvider",
    "CacheTokens",
    "ProviderCapabilities",
    "ProviderResponse",
    "RateLimitedError",
    "StreamEvent",
    "Usage",
]
```

- [ ] **Step 4: Re-export from plugin_sdk/__init__.py**

Add to the from-import block and `__all__` list:

```python
from plugin_sdk.provider_contract import (
    # ...existing names...
    CacheTokens,
    ProviderCapabilities,
)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_provider_capabilities.py -q
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/plugin_sdk/provider_contract.py OpenComputer/plugin_sdk/__init__.py OpenComputer/tests/test_provider_capabilities.py
git commit -m "feat(sdk): add ProviderCapabilities + CacheTokens to plugin_sdk"
```

---

## Task 2: Add `reasoning_replay_blocks` to Message + ProviderResponse

The Anthropic API requires the verbatim thinking block (with signature) to accompany tool_use during a tool-use cycle. Today we capture only the text — the signature is dropped. This task adds the storage. Task 4 wires extraction; Task 5 wires reconstruction.

**Files:**
- Modify: `OpenComputer/plugin_sdk/core.py` (Message dataclass)
- Modify: `OpenComputer/plugin_sdk/provider_contract.py` (ProviderResponse dataclass)
- Test: `OpenComputer/tests/test_reasoning_replay_blocks.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reasoning_replay_blocks.py
"""Message + ProviderResponse can carry verbatim provider-side reasoning blocks."""
from plugin_sdk import Message, ProviderResponse, Usage


def test_message_default_no_replay_blocks():
    m = Message(role="assistant", content="hi")
    assert m.reasoning_replay_blocks is None


def test_message_can_carry_replay_blocks():
    blocks = [{"type": "thinking", "thinking": "let me think", "signature": "abc..."}]
    m = Message(role="assistant", content="", reasoning_replay_blocks=blocks)
    assert m.reasoning_replay_blocks == blocks


def test_provider_response_default_no_replay_blocks():
    r = ProviderResponse(
        message=Message(role="assistant", content="ok"),
        stop_reason="end_turn",
        usage=Usage(),
    )
    assert r.reasoning_replay_blocks is None


def test_provider_response_can_carry_replay_blocks():
    blocks = [{"type": "thinking", "thinking": "...", "signature": "sig"}]
    r = ProviderResponse(
        message=Message(role="assistant", content=""),
        stop_reason="tool_use",
        usage=Usage(),
        reasoning_replay_blocks=blocks,
    )
    assert r.reasoning_replay_blocks == blocks
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_reasoning_replay_blocks.py -q
```
Expected: AttributeError / TypeError — field doesn't exist.

- [ ] **Step 3: Add the field to Message**

In `plugin_sdk/core.py`, locate the Message dataclass (line 19-59). Add a new field at the end of the existing optional fields, just before `attachments`:

```python
    reasoning_replay_blocks: Any = None  # list[dict[str, Any]] | None
    """Verbatim provider-side reasoning blocks that must be replayed
    on the next turn for the provider's reasoning continuity contract.
    Anthropic populates this with thinking blocks (each with a
    ``signature``) when extended thinking is on; the provider's
    message-conversion layer reconstructs them on resend so the API's
    cryptographic-signature check passes during tool-use cycles.
    Other providers leave this ``None``.
    """
```

- [ ] **Step 4: Add the field to ProviderResponse**

In `plugin_sdk/provider_contract.py`, locate `ProviderResponse` (line 33-55). Add a new optional field after `codex_reasoning_items`:

```python
    reasoning_replay_blocks: Any = None  # list[dict[str, Any]] | None
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_reasoning_replay_blocks.py -q
```
Expected: 4 passed.

- [ ] **Step 6: Run the SDK-boundary test to confirm no regressions**

```bash
pytest tests/test_phase6a.py -q
```
Expected: pass (we didn't touch boundary).

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/plugin_sdk/core.py OpenComputer/plugin_sdk/provider_contract.py OpenComputer/tests/test_reasoning_replay_blocks.py
git commit -m "feat(sdk): add reasoning_replay_blocks to Message + ProviderResponse"
```

---

## Task 3: Add `capabilities` property to `BaseProvider` with safe default

**Files:**
- Modify: `OpenComputer/plugin_sdk/provider_contract.py` (BaseProvider class)
- Test: `OpenComputer/tests/test_provider_capabilities.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_provider_capabilities.py`:

```python
def test_base_provider_default_capabilities():
    """A provider that doesn't override .capabilities returns the safe default."""
    from plugin_sdk import BaseProvider, ProviderCapabilities

    class _StubProvider(BaseProvider):
        name = "_stub"
        default_model = "stub-1"

        async def complete(self, **_kw):  # type: ignore[override]
            raise NotImplementedError

        async def stream_complete(self, **_kw):  # type: ignore[override]
            raise NotImplementedError

    caps = _StubProvider().capabilities
    assert isinstance(caps, ProviderCapabilities)
    assert caps.requires_reasoning_resend_in_tool_cycle is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_provider_capabilities.py::test_base_provider_default_capabilities -q
```
Expected: AttributeError — `.capabilities` does not exist.

- [ ] **Step 3: Add the property to BaseProvider**

In `plugin_sdk/provider_contract.py`, inside the `BaseProvider` class (around line 100), add after the abstract methods:

```python
    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declares what this provider supports for the agent loop's
        context-economy decisions. Override in concrete providers that
        opt in to reasoning resend, cache-token extraction, etc. The
        default returns the safe-baseline (everything off), so existing
        providers behave exactly as today until they explicitly opt in.
        """
        return ProviderCapabilities()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_provider_capabilities.py -q
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/plugin_sdk/provider_contract.py OpenComputer/tests/test_provider_capabilities.py
git commit -m "feat(sdk): BaseProvider.capabilities property with safe default"
```

---

## Task 4: Anthropic provider — capability declaration + signature extraction

**Files:**
- Modify: `OpenComputer/extensions/anthropic-provider/provider.py` (around lines 347-391, `_parse_response`)
- Test: `OpenComputer/tests/test_anthropic_capabilities.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_anthropic_capabilities.py
"""Anthropic provider declares its capabilities + extracts thinking signatures."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.anthropic_provider  # convention; ignore if not used


def _import_provider():
    """Import the anthropic-provider plugin module despite hyphenated path."""
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_anth_provider", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _import_provider()
    return mod.AnthropicProvider()


def test_anthropic_capabilities(monkeypatch):
    provider = _build_provider(monkeypatch)
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is True
    assert caps.reasoning_block_kind == "anthropic_thinking"
    assert caps.supports_long_ttl is True
    # Min cache tokens: model-aware
    assert caps.min_cache_tokens("claude-opus-4-7") == 4096
    assert caps.min_cache_tokens("claude-sonnet-4-6") == 2048
    assert caps.min_cache_tokens("claude-sonnet-4-5") == 1024


def test_anthropic_extract_cache_tokens(monkeypatch):
    provider = _build_provider(monkeypatch)
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=1234,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1234
    assert ct.write == 200


def test_anthropic_parse_response_captures_thinking_signature(monkeypatch):
    provider = _build_provider(monkeypatch)

    # Synthesize an Anthropic response with a thinking block + tool_use.
    thinking_block = SimpleNamespace(
        type="thinking",
        thinking="step-by-step reasoning",
        signature="sig-abc-123",
    )
    tool_use = SimpleNamespace(
        type="tool_use",
        id="toolu_01",
        name="Read",
        input={"path": "/etc/hosts"},
    )
    fake_resp = SimpleNamespace(
        content=[thinking_block, tool_use],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    parsed = provider._parse_response(fake_resp)
    assert parsed.reasoning == "step-by-step reasoning"
    assert parsed.reasoning_replay_blocks == [
        {"type": "thinking", "thinking": "step-by-step reasoning", "signature": "sig-abc-123"}
    ]
    # The signature must also have been propagated to the canonical Message
    # so SessionDB persists it.
    assert parsed.message.reasoning_replay_blocks == parsed.reasoning_replay_blocks
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_capabilities.py -q
```
Expected: AttributeError on `.capabilities`, missing `reasoning_replay_blocks` on parsed response.

- [ ] **Step 3: Add `capabilities` property to `AnthropicProvider`**

In `extensions/anthropic-provider/provider.py`, inside the `AnthropicProvider` class (after `__init__`, before `_to_anthropic_messages`):

```python
    # ─── capabilities ───────────────────────────────────────────────

    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage):
            return CacheTokens(
                read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                write=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            )

        def _min_tokens(model: str) -> int:
            m = model.lower()
            if any(tag in m for tag in ("opus", "mythos", "haiku-4-5", "haiku-4.5")):
                return 4096
            if "sonnet-4-6" in m or "sonnet-4.6" in m:
                return 2048
            return 1024

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=True,
            reasoning_block_kind="anthropic_thinking",
            extracts_cache_tokens=_extract,
            min_cache_tokens=_min_tokens,
            supports_long_ttl=True,
        )
```

- [ ] **Step 4: Update `_parse_response` to capture signatures**

Locate `_parse_response` (currently lines ~347-391). Modify the loop and the constructed `Message` / `ProviderResponse`:

```python
    def _parse_response(self, resp: AnthropicMessage) -> ProviderResponse:
        """Convert an Anthropic response back to our canonical Message + metadata."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_parts: list[str] = []
        replay_blocks: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_text = getattr(block, "thinking", None)
                signature = getattr(block, "signature", None)
                if thinking_text:
                    thinking_parts.append(str(thinking_text))
                # Preserve the verbatim block (with signature) so we can
                # replay it on the next turn during the tool-use cycle.
                # The Anthropic API rejects modified or missing signatures.
                if thinking_text is not None and signature is not None:
                    replay_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": str(thinking_text),
                            "signature": str(signature),
                        }
                    )
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )
        replay = replay_blocks or None
        msg = Message(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            reasoning_replay_blocks=replay,
        )
        usage = Usage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0),
        )
        reasoning = "\n".join(thinking_parts) if thinking_parts else None
        return ProviderResponse(
            message=msg,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
            reasoning=reasoning,
            reasoning_replay_blocks=replay,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_capabilities.py -q
```
Expected: 3 passed.

- [ ] **Step 6: Run existing Anthropic provider tests for regressions**

```bash
pytest tests/ -q -k "anthropic" --no-header
```
Expected: pre-existing tests still pass (we added fields with defaults; no behavior change for callers that don't read the new field).

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_capabilities.py
git commit -m "feat(anthropic): declare capabilities + capture thinking-block signatures"
```

---

## Task 5: Anthropic provider — reconstruct thinking blocks in `_to_anthropic_messages`

This closes the bug. When an assistant message with `tool_calls` and `reasoning_replay_blocks` is converted back to wire format, prepend the verbatim thinking blocks before the tool_use block.

**Files:**
- Modify: `OpenComputer/extensions/anthropic-provider/provider.py` (lines 250-301, `_to_anthropic_messages`)
- Test: `OpenComputer/tests/test_anthropic_thinking_resend.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_anthropic_thinking_resend.py
"""Anthropic provider's _to_anthropic_messages reconstructs thinking blocks
during tool-use cycles per the API's signature contract."""
import importlib.util
from pathlib import Path

import pytest

from plugin_sdk import Message, ToolCall


def _import_provider():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_anth_provider_resend", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return _import_provider().AnthropicProvider()


def test_thinking_block_emitted_before_tool_use(provider):
    """Assistant message with tool_calls + reasoning_replay_blocks must
    emit the thinking block on the wire before the tool_use block."""
    msg = Message(
        role="assistant",
        content="reading file now",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "I should read this file", "signature": "sig-xyz"}
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    assert len(wire) == 1
    blocks = wire[0]["content"]
    # Thinking block must come first — the API checks ordering.
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "I should read this file"
    assert blocks[0]["signature"] == "sig-xyz"
    # Then text (if any), then tool_use.
    types_after = [b["type"] for b in blocks[1:]]
    assert "tool_use" in types_after


def test_no_thinking_block_when_no_tool_use(provider):
    """Plain assistant text with reasoning_replay_blocks but no tool_calls
    must NOT emit a thinking block (server auto-handles non-cycle turns)."""
    msg = Message(
        role="assistant",
        content="hi",
        tool_calls=None,
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "...", "signature": "sig"}
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    # The fall-through branch produces a plain text content (no list of blocks).
    assert wire[0]["content"] == "hi"


def test_no_thinking_block_when_replay_blocks_absent(provider):
    """Tool-use message without reasoning_replay_blocks emits today's shape:
    optional text + tool_use, no thinking block."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=None,
    )
    wire = provider._to_anthropic_messages([msg])
    blocks = wire[0]["content"]
    types = [b["type"] for b in blocks]
    assert "thinking" not in types
    assert "tool_use" in types


def test_multiple_thinking_blocks_preserved_in_order(provider):
    """Rare but valid: a response with multiple thinking blocks must emit
    all of them in their original order, before tool_use."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="tu_1", name="Read", arguments={"path": "/x"})],
        reasoning_replay_blocks=[
            {"type": "thinking", "thinking": "first", "signature": "s1"},
            {"type": "thinking", "thinking": "second", "signature": "s2"},
        ],
    )
    wire = provider._to_anthropic_messages([msg])
    blocks = wire[0]["content"]
    assert blocks[0]["thinking"] == "first"
    assert blocks[1]["thinking"] == "second"
    assert blocks[2]["type"] == "tool_use"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_thinking_resend.py -q
```
Expected: thinking blocks not emitted; first test fails.

- [ ] **Step 3: Patch `_to_anthropic_messages`**

In `extensions/anthropic-provider/provider.py`, locate the `assistant + tool_calls` branch (lines ~258-271). Replace with:

```python
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                # If the message carries verbatim reasoning blocks (Anthropic
                # extended thinking with signatures), they MUST be emitted
                # before the tool_use block. The API verifies signatures
                # during the tool-use cycle; missing or out-of-order
                # thinking blocks break reasoning continuity.
                replay = m.reasoning_replay_blocks
                if replay:
                    for blk in replay:
                        # Defensive: only forward thinking blocks we know
                        # how to send. Other shapes (future provider
                        # extensions) are skipped here, not dropped from
                        # the canonical Message.
                        if isinstance(blk, dict) and blk.get("type") == "thinking":
                            content.append(
                                {
                                    "type": "thinking",
                                    "thinking": blk.get("thinking", ""),
                                    "signature": blk.get("signature", ""),
                                }
                            )
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": content})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_thinking_resend.py -q
```
Expected: 4 passed.

- [ ] **Step 5: Run existing tests for regressions**

```bash
pytest tests/ -q -k "anthropic or message" --no-header
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_thinking_resend.py
git commit -m "fix(anthropic): reconstruct thinking blocks before tool_use on resend"
```

---

## Task 5b: SessionDB migration — persist `reasoning_replay_blocks`

The `Message.reasoning_replay_blocks` field added in Task 2 will be silently dropped on persist unless we add a SessionDB column. State.py hard-codes column lists rather than auto-iterating dataclass fields. This matters for mid-cycle session resume (rare but real).

**Files:**
- Modify: `OpenComputer/opencomputer/agent/state.py` (schema, migration, insert SQL, select SQL, deserialization)
- Test: `OpenComputer/tests/test_session_db_replay_blocks.py` (new)

- [ ] **Step 1: Read state.py around the existing reasoning_details columns**

```bash
grep -n "reasoning_details\|codex_reasoning_items" opencomputer/agent/state.py
```

Note the v1→v2 ALTER block (around line 248-255), the insert binding (around line 827-862), and the select projection (around line 939-956).

- [ ] **Step 2: Write failing test**

```python
# tests/test_session_db_replay_blocks.py
"""SessionDB persists Message.reasoning_replay_blocks across save+load."""
from opencomputer.agent.state import SessionDB
from plugin_sdk import Message


def test_replay_blocks_roundtrip(tmp_path):
    db = SessionDB(tmp_path / "test.db")
    session_id = db.create_session()
    blocks = [
        {"type": "thinking", "thinking": "let me work through this", "signature": "sig-roundtrip"}
    ]
    msg = Message(
        role="assistant",
        content="working on it",
        reasoning_replay_blocks=blocks,
    )
    db.add_message(session_id, msg)
    loaded = db.get_messages(session_id)
    assert len(loaded) == 1
    assert loaded[0].reasoning_replay_blocks == blocks
```

(Adjust method names — `create_session` / `add_message` / `get_messages` — to match what state.py actually exposes; read the file to confirm.)

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_session_db_replay_blocks.py -q
```
Expected: persisted row missing column / loaded message has `reasoning_replay_blocks=None`.

- [ ] **Step 4: Add the schema column + migration**

In state.py, find the `messages` CREATE TABLE block. Add:

```sql
    reasoning_replay_blocks TEXT,   -- 2026-05-02: JSON, provider-side reasoning blocks for verbatim replay
```

In the v1→v2 migration block (or add a v2→v3 if v2 was the previous), add the new column:

```python
for col_name in ("reasoning_details", "codex_reasoning_items", "attachments", "reasoning_replay_blocks"):
    # ALTER TABLE if missing
    ...
```

In the auto-migration list (search for `("messages", "reasoning_details", "TEXT")`):

```python
    ("messages", "reasoning_details", "TEXT"),
    ("messages", "codex_reasoning_items", "TEXT"),
    ("messages", "reasoning_replay_blocks", "TEXT"),  # NEW
```

- [ ] **Step 5: Update insert binding**

Find the section that builds `reasoning_details_json`. Add an analogous `reasoning_replay_blocks_json`. Add it to the INSERT column list and value tuple.

- [ ] **Step 6: Update select projection + deserialization**

Find the SELECT statement listing `reasoning, reasoning_details, codex_reasoning_items, attachments`. Add `, reasoning_replay_blocks`. In the row-to-Message construction, deserialize the JSON column and pass to `Message(...)`.

- [ ] **Step 7: Run test to verify it passes**

```bash
pytest tests/test_session_db_replay_blocks.py -q
```
Expected: 1 passed.

- [ ] **Step 8: Run existing state-db tests to confirm no regressions**

```bash
pytest tests/ -q -k "state or session_db" --no-header
```

- [ ] **Step 9: Commit**

```bash
git add opencomputer/agent/state.py tests/test_session_db_replay_blocks.py
git commit -m "feat(state): persist Message.reasoning_replay_blocks for mid-cycle resume"
```

---

## Task 6: OpenAI provider capabilities + `cached_tokens` extraction

**Files:**
- Modify: `OpenComputer/extensions/openai-provider/provider.py`
- Test: `OpenComputer/tests/test_openai_capabilities.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_openai_capabilities.py
"""OpenAI provider declares its capabilities + extracts cached_tokens."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "openai-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_openai_provider", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return _import_provider().OpenAIProvider()


def test_openai_capabilities(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
    assert caps.supports_long_ttl is False
    assert caps.min_cache_tokens("gpt-4o") == 1024


def test_openai_extract_cached_tokens(provider):
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1700),
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1700
    assert ct.write == 0


def test_openai_extract_cached_tokens_missing_field(provider):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_openai_capabilities.py -q
```
Expected: AttributeError on `.capabilities`.

- [ ] **Step 3: Add `capabilities` property to `OpenAIProvider`**

Find the `OpenAIProvider` class definition. Insert after `__init__`:

```python
    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage):
            details = getattr(usage, "prompt_tokens_details", None)
            cached = 0
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
            return CacheTokens(read=cached, write=0)

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=False,
            reasoning_block_kind=None,
            extracts_cache_tokens=_extract,
            min_cache_tokens=lambda _model: 1024,
            supports_long_ttl=False,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_openai_capabilities.py -q
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/openai-provider/provider.py OpenComputer/tests/test_openai_capabilities.py
git commit -m "feat(openai): declare capabilities + cached_tokens extraction"
```

---

## Task 7: OpenRouter provider capabilities + dual-shape extraction

**Files:**
- Modify: `OpenComputer/extensions/openrouter-provider/provider.py`
- Test: `OpenComputer/tests/test_openrouter_capabilities.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_openrouter_capabilities.py
"""OpenRouter provider's cache-token extractor reads either Anthropic-style
or OpenAI-style fields, depending on which upstream answered."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "openrouter-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_or_provider", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    mod = _import_provider()
    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and k.endswith("Provider") and v.__module__ == mod.__name__
    )
    return cls()


def test_openrouter_capabilities_safe_defaults(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.supports_long_ttl is False


def test_openrouter_extracts_anthropic_shape(provider):
    """When OpenRouter routes to Anthropic, usage carries Anthropic field names."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=1500,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1500
    assert ct.write == 200


def test_openrouter_extracts_openai_shape(provider):
    """When OpenRouter routes to an OpenAI-compatible upstream."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=900),
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 900
    assert ct.write == 0


def test_openrouter_no_cache_fields(provider):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_openrouter_capabilities.py -q
```
Expected: AttributeError on `.capabilities`.

- [ ] **Step 3: Add `capabilities` property to OpenRouter provider**

Locate the OpenRouter provider class definition. Insert after `__init__`:

```python
    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage):
            # Prefer Anthropic-shape fields (more specific); fall back to
            # OpenAI-shape. OpenRouter passes the upstream's payload through.
            anth_read = getattr(usage, "cache_read_input_tokens", None)
            anth_write = getattr(usage, "cache_creation_input_tokens", None)
            if anth_read is not None or anth_write is not None:
                return CacheTokens(
                    read=int(anth_read or 0),
                    write=int(anth_write or 0),
                )
            details = getattr(usage, "prompt_tokens_details", None)
            cached = 0
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
            return CacheTokens(read=cached, write=0)

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=False,
            reasoning_block_kind=None,
            extracts_cache_tokens=_extract,
            min_cache_tokens=lambda _model: 1024,
            supports_long_ttl=False,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_openrouter_capabilities.py -q
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/openrouter-provider/provider.py OpenComputer/tests/test_openrouter_capabilities.py
git commit -m "feat(openrouter): declare capabilities + dual-shape cache extraction"
```

---

## Task 8: Gemini provider capabilities + `cached_content_token_count` extraction

**Files:**
- Modify: `OpenComputer/extensions/gemini-provider/provider.py`
- Test: `OpenComputer/tests/test_gemini_capabilities.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_gemini_capabilities.py
"""Gemini provider declares capabilities + extracts cached_content_token_count."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "gemini-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_gemini_provider", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    # Gemini key naming may vary; set both common forms.
    monkeypatch.setenv("GEMINI_API_KEY", "key-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "key-test")
    mod = _import_provider()
    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and k.endswith("Provider") and v.__module__ == mod.__name__
    )
    return cls()


def test_gemini_capabilities_safe_defaults(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.supports_long_ttl is False


def test_gemini_extracts_cached_content_token_count(provider):
    usage = SimpleNamespace(
        prompt_token_count=2000,
        candidates_token_count=100,
        cached_content_token_count=1500,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1500
    assert ct.write == 0


def test_gemini_no_cached_content(provider):
    usage = SimpleNamespace(prompt_token_count=2000, candidates_token_count=100)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_gemini_capabilities.py -q
```

- [ ] **Step 3: Add `capabilities` property to Gemini provider**

Insert after `__init__` of the Gemini provider class:

```python
    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage):
            cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
            return CacheTokens(read=cached, write=0)

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=False,
            reasoning_block_kind=None,
            extracts_cache_tokens=_extract,
            min_cache_tokens=lambda _model: 1024,
            supports_long_ttl=False,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_gemini_capabilities.py -q
```

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/extensions/gemini-provider/provider.py OpenComputer/tests/test_gemini_capabilities.py
git commit -m "feat(gemini): declare capabilities + cached_content_token_count extraction"
```

---

## Task 9: prompt_caching.py — capability-aware size-threshold filter

**Files:**
- Modify: `OpenComputer/opencomputer/agent/prompt_caching.py`
- Test: `OpenComputer/tests/test_prompt_caching_thresholds.py` (new)

Read the existing module first; the change is small but the tests need to mirror its message-list shape.

- [ ] **Step 1: Read prompt_caching.py end-to-end**

```bash
cat OpenComputer/opencomputer/agent/prompt_caching.py
```

Note the function signature of `apply_anthropic_cache_control` and the shape of `_apply_cache_marker` so the test fixtures match.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_prompt_caching_thresholds.py
"""Cache markers must be skipped on blocks below the provider's threshold."""
from opencomputer.agent.prompt_caching import apply_anthropic_cache_control


def _msg(text):
    return {"role": "user", "content": text}


def test_below_threshold_block_skipped():
    """A 200-character (~50-token) block on a 4096-token-min model must
    not receive a cache_control marker — that would be a silent no-op
    that wastes a breakpoint slot."""
    short = "x" * 200
    long = "y" * (5 * 4096)  # ~5k tokens, well above threshold
    msgs = [_msg(long), _msg(short)]
    out = apply_anthropic_cache_control(
        msgs,
        native_anthropic=False,
        min_cache_tokens=4096,
    )
    # The short message must not carry cache_control; the long one must.
    assert "cache_control" not in str(out[1].get("content", ""))


def test_threshold_zero_marks_everything():
    """Default min=0 preserves today's behaviour: every candidate gets marked."""
    msgs = [_msg("a" * 100), _msg("b" * 100), _msg("c" * 100)]
    out = apply_anthropic_cache_control(msgs, native_anthropic=False)
    # At least one of the last 3 must have cache_control somewhere.
    found = any("cache_control" in str(m.get("content")) for m in out)
    assert found
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_prompt_caching_thresholds.py -q
```

- [ ] **Step 4: Add `min_cache_tokens` parameter to `apply_anthropic_cache_control`**

Modify `opencomputer/agent/prompt_caching.py`. Add `min_cache_tokens: int = 0` to the function signature. In the loop that places markers, before applying, check the candidate block's text length:

```python
def _block_token_estimate(content) -> int:
    """Cheap upper-bound token count — 4 chars per token is a reasonable
    over-estimate that errs on the side of "let it through". This is used
    only for cache-marker eligibility, never for billing.
    """
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        # Sum text-block character counts.
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                total += len(block.get("text", ""))
        return total // 4
    return 0
```

In the existing breakpoint-placement loop, when iterating over `non_sys[-remaining:]`, check `_block_token_estimate` against `min_cache_tokens`. If under, walk back one more position; up to **20 positions** (matches Anthropic's server-side lookback window) before giving up on that breakpoint slot.

The exact insertion point is the loop that calls `_apply_cache_marker`. Add the threshold filter inline.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_prompt_caching_thresholds.py -q
```

- [ ] **Step 6: Run existing prompt-caching tests for regressions**

```bash
pytest tests/ -q -k "cache" --no-header
```

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/agent/prompt_caching.py OpenComputer/tests/test_prompt_caching_thresholds.py
git commit -m "feat(prompt_caching): skip cache markers on sub-threshold blocks"
```

---

## Task 10: prompt_caching.py — idle-aware TTL switch

**Files:**
- Modify: `OpenComputer/opencomputer/agent/prompt_caching.py`
- Modify: `OpenComputer/extensions/anthropic-provider/provider.py` (call site at line ~331)
- Test: `OpenComputer/tests/test_idle_ttl_switch.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_idle_ttl_switch.py
"""When a turn lands more than 4 minutes after the previous one and the
provider supports long TTL, the cache TTL flips to '1h'."""
from opencomputer.agent.prompt_caching import select_cache_ttl


def test_short_gap_default_ttl():
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=60.0) == "5m"


def test_long_gap_long_ttl_when_supported():
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=300.0) == "1h"


def test_long_gap_default_when_unsupported():
    assert select_cache_ttl(supports_long_ttl=False, idle_seconds=600.0) == "5m"


def test_threshold_boundary():
    # 4 minutes = 240s — exactly at threshold rounds DOWN to 5m to be conservative.
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=240.0) == "5m"
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=240.1) == "1h"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_idle_ttl_switch.py -q
```

- [ ] **Step 3: Add `select_cache_ttl` helper to prompt_caching.py**

Append to `opencomputer/agent/prompt_caching.py`:

```python
_LONG_TTL_THRESHOLD_SECONDS = 240.0  # 4 minutes — leaves 1m safety below 5m TTL


def select_cache_ttl(*, supports_long_ttl: bool, idle_seconds: float) -> str:
    """Decide between '5m' (default) and '1h' (long) cache TTL.

    Returns ``"1h"`` only when:
      * the provider declares ``supports_long_ttl`` True, AND
      * the gap since the last assistant turn exceeds 4 minutes.

    The 4-minute threshold leaves a one-minute safety buffer below the
    default 5-minute cache lifetime, so a session that pauses for 5+
    minutes would otherwise pay a full re-prefill on the next turn.
    """
    if not supports_long_ttl:
        return "5m"
    if idle_seconds > _LONG_TTL_THRESHOLD_SECONDS:
        return "1h"
    return "5m"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_idle_ttl_switch.py -q
```

- [ ] **Step 4.5: Enumerate `_apply_cache_control` callers**

```bash
grep -n "_apply_cache_control" extensions/anthropic-provider/provider.py
```

List every call site. They all need the new kwargs (`idle_seconds`, `model`). If the kwargs default safely (0.0 / ""), un-updated callers still compile; but we want to update each to pass the real values.

- [ ] **Step 5: Wire idle-gap measurement using a per-provider attribute**

Track the wall-clock time of the last call as a per-provider instance attribute. Simpler than threading through `runtime_extras` (which is reserved for `reasoning_effort`/`service_tier` per the BaseProvider docstring) and naturally covers both `complete` and `stream_complete`.

In `__init__`, add:

```python
import time
self._last_call_ts: float = 0.0
```

In each entry point (`_do_complete`, `_do_stream_complete`), at the top:

```python
import time
now = time.monotonic()
idle_seconds = (now - self._last_call_ts) if self._last_call_ts > 0 else 0.0
self._last_call_ts = now
```

Pass `idle_seconds` and `model` through to `_apply_cache_control`.

```python
# In _apply_cache_control, accept idle_seconds and pass through:
def _apply_cache_control(
    self,
    anthropic_messages: list[dict[str, Any]],
    system: str,
    *,
    idle_seconds: float = 0.0,
    model: str = "",
) -> tuple[Any, list[dict[str, Any]]]:
    from opencomputer.agent.prompt_caching import select_cache_ttl
    ttl = select_cache_ttl(
        supports_long_ttl=self.capabilities.supports_long_ttl,
        idle_seconds=idle_seconds,
    )
    # ... existing logic, passing cache_ttl=ttl into apply_anthropic_cache_control
```

And in `apply_anthropic_cache_control`, accept `cache_ttl: str = "5m"` and forward into `_apply_cache_marker`.

- [ ] **Step 6: Run integration test to verify wiring**

Add to `tests/test_idle_ttl_switch.py`:

```python
def test_anthropic_provider_passes_long_ttl_when_idle(monkeypatch):
    """End-to-end: an Anthropic provider call with idle_seconds=600 places
    cache_control with ttl='1h' on the wire payload."""
    import importlib.util
    from pathlib import Path
    from plugin_sdk import Message

    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location("_anth_ttl", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    provider = mod.AnthropicProvider()

    # Build messages large enough to clear the threshold filter.
    big = "x" * (5 * 4096)
    msgs = [Message(role="user", content=big)]
    anth_msgs = provider._to_anthropic_messages(msgs)
    sys_for_sdk, msgs_for_sdk = provider._apply_cache_control(
        anth_msgs, system="", idle_seconds=600.0, model="claude-opus-4-7"
    )

    # Find any cache_control marker in the resulting payload.
    payload = str(msgs_for_sdk) + str(sys_for_sdk)
    assert "1h" in payload, f"expected '1h' TTL in payload, got: {payload[:500]}"
```

- [ ] **Step 7: Run tests**

```bash
pytest tests/test_idle_ttl_switch.py -q
```

- [ ] **Step 8: Commit**

```bash
git add OpenComputer/opencomputer/agent/prompt_caching.py OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_idle_ttl_switch.py
git commit -m "feat(prompt_caching): idle-aware 1h TTL switch when supported"
```

---

## Task 11: StepOutcome + telemetry — propagate cache tokens

**Files:**
- Modify: `OpenComputer/opencomputer/agent/step.py` (StepOutcome dataclass)
- Modify: `OpenComputer/opencomputer/agent/loop.py` (the `StepOutcome(...)` construction site, ~line 2820)
- Test: `OpenComputer/tests/test_step_outcome_cache_fields.py` (new)

- [ ] **Step 1: Read existing StepOutcome definition**

```bash
grep -n "StepOutcome" OpenComputer/opencomputer/agent/step.py | head
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_step_outcome_cache_fields.py
"""StepOutcome carries cache_read_tokens and cache_write_tokens."""
from opencomputer.agent.step import StepOutcome


def test_step_outcome_default_cache_zero():
    out = StepOutcome(input_tokens=10, output_tokens=5)
    assert out.cache_read_tokens == 0
    assert out.cache_write_tokens == 0


def test_step_outcome_carries_cache_tokens():
    out = StepOutcome(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=1234,
        cache_write_tokens=200,
    )
    assert out.cache_read_tokens == 1234
    assert out.cache_write_tokens == 200
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_step_outcome_cache_fields.py -q
```

- [ ] **Step 4: Add fields to StepOutcome**

In `opencomputer/agent/step.py`, add to the StepOutcome dataclass:

```python
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
```

- [ ] **Step 5: Wire population at the construction site in loop.py**

Find the `StepOutcome(` construction (around line 2820). Add the two cache-token kwargs reading from the provider response's `usage`:

```python
    outcome = StepOutcome(
        # ...existing fields...
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cache_read_tokens=resp.usage.cache_read_tokens,
        cache_write_tokens=resp.usage.cache_write_tokens,
    )
```

- [ ] **Step 6: Run test to verify it passes**

```bash
pytest tests/test_step_outcome_cache_fields.py -q
```

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/agent/step.py OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_step_outcome_cache_fields.py
git commit -m "feat(loop): propagate cache tokens into StepOutcome"
```

---

## Task 12: `/usage` CLI surface — cache hit/miss line

**Files:**
- Modify: the `/usage` command renderer (likely in `opencomputer/agent/loop.py` or a slash-commands module — locate via grep)
- Test: `OpenComputer/tests/test_usage_command_cache_line.py` (new)

- [ ] **Step 1: Locate the /usage renderer**

```bash
grep -rn "/usage\|def.*usage_command\|def.*format_usage" OpenComputer/opencomputer/ | head
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_usage_command_cache_line.py
"""/usage shows a cache line when cache tokens are non-zero, omits otherwise."""
# The exact import depends on where /usage lives; adapt to the repo's actual API.

def test_usage_renders_cache_line_when_present():
    from opencomputer.agent.loop import render_usage_summary  # adjust if needed
    out = render_usage_summary(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=12_400,
        cache_write_tokens=880,
        model="claude-opus-4-7",
    )
    assert "Cache" in out
    assert "12,400" in out or "12400" in out
    assert "880" in out


def test_usage_omits_cache_line_when_zero():
    from opencomputer.agent.loop import render_usage_summary
    out = render_usage_summary(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        model="gpt-4o",
    )
    assert "Cache" not in out
```

- [ ] **Step 3: Implement `render_usage_summary` (or extend the existing renderer)**

Locate the existing rendering code (it may be a method on a class or a module-level function). Add a conditional cache line:

```python
def render_usage_summary(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    model: str = "",
) -> str:
    """Render a one-paragraph usage summary for /usage."""
    lines = [
        f"Tokens: {input_tokens:,} in, {output_tokens:,} out",
    ]
    if cache_read_tokens or cache_write_tokens:
        # Estimate dollars saved using the existing pricing table.
        from opencomputer.agent.pricing import dollars_saved_by_cache  # if available
        try:
            saved = dollars_saved_by_cache(
                model=model,
                cache_read=cache_read_tokens,
                cache_write=cache_write_tokens,
            )
            saved_str = f" (≈ saved ${saved:,.2f})"
        except Exception:
            saved_str = ""
        lines.append(
            f"Cache: {cache_read_tokens:,} read / {cache_write_tokens:,} written{saved_str}"
        )
    return "\n".join(lines)
```

If `opencomputer.agent.pricing` doesn't exist or doesn't expose this helper, omit the dollar estimate entirely (the test only checks for token formatting).

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_usage_command_cache_line.py -q
```

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_usage_command_cache_line.py
git commit -m "feat(/usage): show cache hit/miss line and dollar estimate"
```

---

## Task 13: Full test suite + ruff + audit log + push + PR

- [ ] **Step 1: Run the full pytest suite**

```bash
cd OpenComputer
pytest tests/ -q
```
Expected: all green. The memory rule "no push without deep testing" is non-negotiable.

- [ ] **Step 2: Run ruff check**

```bash
ruff check plugin_sdk/ opencomputer/agent/ extensions/anthropic-provider/ extensions/openai-provider/ extensions/openrouter-provider/ extensions/gemini-provider/ tests/ -q
```
Expected: 0 errors.

- [ ] **Step 3: Sanity-check the worktree**

```bash
git status
git log main..HEAD --oneline
```
Expected: clean tree, ~12 commits on the branch.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/provider-context-economy
gh pr create --title "feat: provider-agnostic context economy (cache telemetry + Anthropic thinking-block resend fix)" --body "$(cat <<'EOF'
## Summary
- Adds `ProviderCapabilities` + `CacheTokens` to `plugin_sdk` so providers declare their support for reasoning resend, cache-token extraction, model-specific min-cache thresholds, and long TTL.
- Fixes a real correctness bug: the Anthropic provider's `_to_anthropic_messages()` was dropping thinking blocks on resend, breaking signature verification during tool-use cycles. Now reconstructs blocks verbatim using the captured signature.
- Surfaces cache hit/miss tokens uniformly: `Usage` already had the fields (PR #263); we now propagate to `StepOutcome` and render in `/usage` with a dollar-saved estimate.
- Capability declarations for anthropic, openai, openrouter (dual-shape), gemini providers. ~25 other providers inherit safe defaults — zero behavior change for them.
- `prompt_caching.py` now skips cache markers on sub-threshold blocks (saves breakpoint slots) and switches to 1h TTL when the provider supports it AND the idle gap exceeds 4 minutes.

## What is explicitly NOT in this PR
See `docs/superpowers/specs/2026-05-02-provider-agnostic-context-economy-design.md` §3.

## Test plan
- [ ] `pytest tests/` all green
- [ ] `ruff check` clean
- [ ] Live smoke (off-CI): run an Anthropic Opus 4.7 conversation with extended thinking + a tool call; verify continued reasoning across the cycle
- [ ] Live smoke (off-CI): run an OpenAI gpt-4o conversation; verify `/usage` shows non-zero cache reads on the second identical request

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Update memory**

After PR is opened, append a project memory note via the user's auto-memory system noting the PR number and date.

---

## Self-Review (post-audit)

Spec coverage:

- §4.1 ProviderCapabilities → Tasks 1, 3 ✓
- §4.2 Usage extension → already in HEAD (PR #263); plan acknowledges this ✓
- §4.3 Message extension (renamed `reasoning_signature` → `reasoning_replay_blocks` to handle multiple blocks per response) → Task 2 ✓
- §4.4 Anthropic thinking-block reconstruction → Tasks 4 + 5 ✓
- §4.5 prompt_caching capability awareness → Tasks 9 + 10 ✓
- §4.6 Telemetry surface → Tasks 11 + 12 ✓

Provider declarations: Anthropic (Task 4), OpenAI (6), OpenRouter (7), Gemini (8). ~25 others stay on safe defaults — exactly as the spec says.

### Audit fixes applied (during self-review pass)

1. **Streaming-path coverage** — verified that `_do_stream_complete` ends with `_parse_response(final)`, so the Task 4 fix to `_parse_response` covers both streaming and non-streaming paths. No separate streaming task needed.
2. **SessionDB persistence** — confirmed `state.py` hard-codes column lists (does not auto-iterate dataclass fields). Added **Task 5b** to migrate the schema (new `reasoning_replay_blocks TEXT` column, ALTER, insert binding, select projection, deserialization) so mid-cycle session resume still has the signatures.
3. **`_apply_cache_control` callers** — Task 10 now has an explicit step (4.5) to enumerate every call site before the signature change, avoiding silent breakage.
4. **Walk-back limit** — corrected from "up to 8" to **20** (matches Anthropic's server-side lookback window).
5. **Idle-time source** — switched from threading via `runtime_extras` (reserved for runtime flags per the BaseProvider docstring) to a per-provider `_last_call_ts` instance attribute, which naturally covers both `complete` and `stream_complete` paths.

### Known acceptable fuzziness

Task 9 step 4 ("the exact insertion point is the loop that calls `_apply_cache_marker`") and Task 12 step 1 ("locate the `/usage` renderer") both require reading the live module before editing. The "read first" step is included in each task, so the executor has the file in context before the insertion. Acceptable.

### Type-name consistency check

- `reasoning_replay_blocks` used identically across Tasks 2, 4, 5, 5b ✓
- `CacheTokens.read` / `.write` consistent across all extractor tests ✓
- `select_cache_ttl` signature matches between Task 10 step 3 and step 5 ✓
- `min_cache_tokens(model)` callable signature matches between Tasks 1, 4, 6, 7, 8, 9 ✓
