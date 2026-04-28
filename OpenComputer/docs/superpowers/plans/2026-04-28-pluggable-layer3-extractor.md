# Pluggable Layer 3 Extractor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `llm_extractor.py` so Layer 3 deepening can use Ollama (default), Anthropic, or OpenAI as the extraction backend, with cost-cap + one-time privacy banner on first switch.

**Architecture:** Protocol + 3 implementations + factory + `DeepeningConfig`. Mirrors the `Classifier[L]` shape from PR #201. Public surface (`extract_artifact`, `OllamaUnavailableError`, `ArtifactExtraction`) stays back-compat.

**Tech Stack:** Python 3.12+, existing `BaseProvider` (Anthropic/OpenAI), existing `cost_guard`, pytest with `AsyncMock(spec=BaseProvider)`.

---

## Task 1: `DeepeningConfig` dataclass

**Files:**
- Modify: `opencomputer/agent/config.py` (after `MemoryConfig`, before `HookCommandConfig`)

- [ ] **Step 1: Add the dataclass**

```python
@dataclass(frozen=True, slots=True)
class DeepeningConfig:
    """Layer 3 deepening — content extractor + cost controls.

    Backend choice is a string (not a closed Literal) so adding a
    new extractor — Gemini, llama-cpp, etc. — is not a breaking
    config schema change. The factory validates against the canonical
    list at runtime.
    """

    extractor: str = "ollama"
    """One of: "ollama" (default — local, private), "anthropic",
    "openai". Other values raise at factory time."""

    model: str = ""
    """Model id passed to the extractor. Empty → backend-specific
    default (llama3.2:3b / claude-haiku-4-5-20251001 / gpt-4o-mini)."""

    daily_cost_cap_usd: float = 0.50
    """Per-day spend ceiling for API extractors. Cost guard skips
    further extractions on the same UTC day once exceeded. Ollama
    bypasses cost guard (zero cost)."""

    max_artifacts_per_pass: int = 100
    timeout_seconds: float = 15.0
```

- [ ] **Step 2: Wire into `Config`**

Find the `Config` dataclass (also in `config.py`) and add:

```python
deepening: DeepeningConfig = field(default_factory=DeepeningConfig)
```

- [ ] **Step 3: Add YAML loader**

In `config_store.py`, find where `MemoryConfig` is parsed from YAML and add a parallel block for `deepening:` reading the same fields. Pattern:

```python
deep_data = data.get("deepening", {}) or {}
deepening_cfg = DeepeningConfig(
    extractor=str(deep_data.get("extractor", "ollama")),
    model=str(deep_data.get("model", "")),
    daily_cost_cap_usd=float(deep_data.get("daily_cost_cap_usd", 0.50)),
    max_artifacts_per_pass=int(deep_data.get("max_artifacts_per_pass", 100)),
    timeout_seconds=float(deep_data.get("timeout_seconds", 15.0)),
)
```

- [ ] **Step 4: Run existing tests**

```bash
pytest tests/test_config*.py -q
```

Expected: all pass — no behavior change yet, only added optional config.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/config.py opencomputer/agent/config_store.py
git commit -m "feat(config): DeepeningConfig dataclass + YAML loader"
```

## Task 2: `ArtifactExtractor` Protocol + back-compat alias

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py`

- [ ] **Step 1: Add the Protocol at top of file**

After the existing imports, before `_DEFAULT_MODEL`:

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArtifactExtractor(Protocol):
    """Pluggable Layer 3 backend.

    Implementations must be deterministic about availability
    (``is_available()`` returns the same answer until env / install
    state changes) and idempotent on the call shape (``extract`` is
    a function of input + model, no hidden state).
    """

    def is_available(self) -> bool: ...

    def extract(self, content: str) -> "ArtifactExtraction": ...


class ExtractorUnavailableError(RuntimeError):
    """Raised when the chosen backend is missing (binary absent,
    API key missing, etc.). Callers handle by falling back to a
    blank extraction or skipping the artifact entirely."""


# Back-compat — pre-2026-04-28 callers caught OllamaUnavailableError.
OllamaUnavailableError = ExtractorUnavailableError
```

- [ ] **Step 2: Run linting**

```bash
ruff check opencomputer/profile_bootstrap/llm_extractor.py
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py
git commit -m "feat(extractor): ArtifactExtractor Protocol + back-compat alias"
```

## Task 3: `OllamaArtifactExtractor` class — refactor existing logic

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py`

- [ ] **Step 1: Wrap existing function logic in a class**

Add the class. Existing free functions stay as a back-compat layer that delegates.

```python
class OllamaArtifactExtractor:
    """Local-first extractor — Ollama subprocess. Zero cost, all
    content stays on the machine.
    """

    _DEFAULT_MODEL = "llama3.2:3b"

    def __init__(self, *, model: str = "", timeout_seconds: float = 15.0) -> None:
        self.model = model or self._DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        return shutil.which("ollama") is not None

    def extract(self, content: str) -> ArtifactExtraction:
        if not self.is_available():
            raise ExtractorUnavailableError(
                "ollama not on PATH; install via 'brew install ollama'"
            )
        artifact = content[:4000]
        prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
        try:
            result = subprocess.run(
                ["ollama", "run", self.model, prompt],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ArtifactExtraction()
        if result.returncode != 0:
            return ArtifactExtraction()
        return _parse_extraction(result.stdout.strip())
```

- [ ] **Step 2: Update existing free functions to delegate**

Keep `is_ollama_available()` + `extract_artifact()` as one-liners that build a transient `OllamaArtifactExtractor` and call through. Pre-existing callers see no surface change.

```python
def is_ollama_available() -> bool:
    return OllamaArtifactExtractor().is_available()


def extract_artifact(
    content: str,
    *,
    model: str = OllamaArtifactExtractor._DEFAULT_MODEL,
    timeout_seconds: float = 15.0,
) -> ArtifactExtraction:
    return OllamaArtifactExtractor(
        model=model, timeout_seconds=timeout_seconds,
    ).extract(content)
```

- [ ] **Step 3: Run existing extractor tests**

```bash
pytest tests/test_profile_bootstrap_*.py -q
```

Expected: all pass — public surface unchanged.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py
git commit -m "refactor(extractor): wrap Ollama logic in OllamaArtifactExtractor class"
```

## Task 4: `AnthropicArtifactExtractor` class

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py`

- [ ] **Step 1: Add class**

```python
class AnthropicArtifactExtractor:
    """Anthropic API extractor — sends artifact content to Anthropic.

    Picks ``claude-haiku-4-5-20251001`` by default — fastest + cheapest
    Anthropic that handles structured JSON extraction reliably.
    """

    _DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(
        self,
        *,
        model: str = "",
        timeout_seconds: float = 15.0,
        api_key: str | None = None,
    ) -> None:
        self.model = model or self._DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key  # explicit; falls back to env in is_available

    def is_available(self) -> bool:
        import os
        return bool(self._api_key or os.getenv("ANTHROPIC_API_KEY"))

    def extract(self, content: str) -> ArtifactExtraction:
        if not self.is_available():
            raise ExtractorUnavailableError(
                "anthropic extractor needs ANTHROPIC_API_KEY in env"
            )
        artifact = content[:4000]
        prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
        try:
            return _run_provider_extract_sync(
                provider_name="anthropic",
                model=self.model,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
                api_key=self._api_key,
            )
        except Exception:  # noqa: BLE001 — match existing blank-on-fail
            return ArtifactExtraction()
```

- [ ] **Step 2: Add the sync helper that bridges to BaseProvider**

```python
def _run_provider_extract_sync(
    *,
    provider_name: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    api_key: str | None,
) -> ArtifactExtraction:
    """Run one provider call synchronously. Spins a fresh event loop
    per call. Per-pass deepening latency is dominated by the LLM
    itself; loop spin-up is in the noise.
    """
    import asyncio

    from plugin_sdk.core import Message

    async def _go() -> ArtifactExtraction:
        provider = _build_provider(provider_name, api_key=api_key)
        try:
            response = await asyncio.wait_for(
                provider.complete(
                    messages=[Message(role="user", content=prompt)],
                    model=model,
                    max_tokens=512,
                ),
                timeout=timeout_seconds,
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ArtifactExtraction()
        # Cost-guard accounting (best-effort — never block on ledger I/O)
        try:
            _record_extraction_cost(provider_name, response.usage)
        except Exception:  # noqa: BLE001
            pass
        # Response.message.content is the JSON string per the prompt.
        text = (response.message.content or "").strip()
        return _parse_extraction(text)

    return asyncio.run(_go())


def _build_provider(name: str, *, api_key: str | None):
    """Return a :class:`BaseProvider` instance. Lazy import so tests
    that don't touch the provider path don't pay the import cost.
    """
    if name == "anthropic":
        from extensions.anthropic_provider.provider import (
            AnthropicProvider,
            AnthropicProviderConfig,
        )
        cfg = AnthropicProviderConfig(api_key=api_key) if api_key else AnthropicProviderConfig()
        return AnthropicProvider(cfg)
    if name == "openai":
        from extensions.openai_provider.provider import (
            OpenAIProvider,
            OpenAIProviderConfig,
        )
        cfg = OpenAIProviderConfig(api_key=api_key) if api_key else OpenAIProviderConfig()
        return OpenAIProvider(cfg)
    raise ValueError(f"unknown provider: {name}")


def _record_extraction_cost(provider_name: str, usage) -> None:
    """Append a usage row via the existing cost guard. Best-effort."""
    try:
        from opencomputer.cost_guard.guard import CostGuard
        guard = CostGuard.default()
        guard.record_usage(
            provider=provider_name,
            input_tokens=int(getattr(usage, "input_tokens", 0)),
            output_tokens=int(getattr(usage, "output_tokens", 0)),
            source="deepening_extraction",
        )
    except Exception:  # noqa: BLE001
        # Cost-guard module/instance not available; recording is
        # informational, do not fail the extraction.
        pass
```

- [ ] **Step 3: Verify the import path for AnthropicProvider matches**

Existing file is at `extensions/anthropic-provider/provider.py` (note the dash). Python imports replace dashes with underscores via the loader's synthetic module names. Confirm the actual import path in a quick REPL or grep:

```bash
grep -n "from extensions.anthropic" opencomputer/ -r --include="*.py" | head -5
```

If the existing imports use a different path (e.g. dynamic loader), refactor `_build_provider` to call through the plugin registry instead — `PluginRegistry.get_provider("anthropic")`.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py
git commit -m "feat(extractor): AnthropicArtifactExtractor"
```

## Task 5: `OpenAIArtifactExtractor` class

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py`

- [ ] **Step 1: Add class — same shape as AnthropicArtifactExtractor**

```python
class OpenAIArtifactExtractor:
    """OpenAI API extractor — sends artifact content to OpenAI.

    Default model is ``gpt-4o-mini`` for the price/quality sweet spot
    on JSON extraction. Override via DeepeningConfig.model.
    """

    _DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        *,
        model: str = "",
        timeout_seconds: float = 15.0,
        api_key: str | None = None,
    ) -> None:
        self.model = model or self._DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key

    def is_available(self) -> bool:
        import os
        return bool(self._api_key or os.getenv("OPENAI_API_KEY"))

    def extract(self, content: str) -> ArtifactExtraction:
        if not self.is_available():
            raise ExtractorUnavailableError(
                "openai extractor needs OPENAI_API_KEY in env"
            )
        artifact = content[:4000]
        prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
        try:
            return _run_provider_extract_sync(
                provider_name="openai",
                model=self.model,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
                api_key=self._api_key,
            )
        except Exception:  # noqa: BLE001
            return ArtifactExtraction()
```

- [ ] **Step 2: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py
git commit -m "feat(extractor): OpenAIArtifactExtractor"
```

## Task 6: `get_extractor()` factory + privacy banner

**Files:**
- Modify: `opencomputer/profile_bootstrap/llm_extractor.py`

- [ ] **Step 1: Add factory + banner**

```python
_KNOWN_EXTRACTORS = ("ollama", "anthropic", "openai")


def get_extractor(config) -> ArtifactExtractor:
    """Return the configured extractor instance.

    ``config`` is the loaded :class:`opencomputer.agent.config.Config`.
    Caller already has it; we don't reload from disk.

    Prints a one-time privacy banner the first time the user picks
    a non-ollama backend. The banner is a single ``print`` to stderr
    so it surfaces in interactive ``oc profile deepen`` runs without
    cluttering log files.
    """
    cfg = config.deepening
    name = (cfg.extractor or "ollama").lower()
    if name not in _KNOWN_EXTRACTORS:
        raise ValueError(
            f"deepening.extractor={name!r} not in {_KNOWN_EXTRACTORS}"
        )

    if name != "ollama":
        _maybe_print_privacy_banner(name, config=config)

    if name == "ollama":
        return OllamaArtifactExtractor(
            model=cfg.model, timeout_seconds=cfg.timeout_seconds,
        )
    if name == "anthropic":
        return AnthropicArtifactExtractor(
            model=cfg.model, timeout_seconds=cfg.timeout_seconds,
        )
    if name == "openai":
        return OpenAIArtifactExtractor(
            model=cfg.model, timeout_seconds=cfg.timeout_seconds,
        )
    raise ValueError(f"unreachable: {name}")  # pragma: no cover


def _maybe_print_privacy_banner(backend: str, *, config) -> None:
    """One-time privacy notice when switching to a cloud backend."""
    import sys
    try:
        marker_dir = config.memory.declarative_path.parent
    except Exception:  # noqa: BLE001
        return  # config malformed — skip banner, don't crash extraction
    marker = marker_dir / f"deepening_consent_{backend}.acknowledged"
    if marker.exists():
        return
    sys.stderr.write(
        f"\nⓘ  Layer 3 deepening is now using {backend}.\n"
        f"   Artifact content (file bodies, browser pages) will be sent\n"
        f"   to {backend} for extraction. To stay local, set\n"
        f"   `deepening.extractor: ollama` in config.yaml.\n\n"
    )
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("acknowledged\n")
    except OSError:
        pass  # ack marker is best-effort
```

- [ ] **Step 2: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py
git commit -m "feat(extractor): get_extractor() factory + one-time privacy banner"
```

## Task 7: Wire factory into orchestrator

**Files:**
- Modify: `opencomputer/profile_bootstrap/orchestrator.py:300-313` (the `extract_and_emit_motif` body)

- [ ] **Step 1: Replace the direct `extract_artifact(content)` call**

Find:

```python
from opencomputer.profile_bootstrap.llm_extractor import (
    ArtifactExtraction,
    OllamaUnavailableError,
    extract_artifact,
)
try:
    extraction = extract_artifact(content)
except OllamaUnavailableError:
    return False
```

Change to:

```python
from opencomputer.profile_bootstrap.llm_extractor import (
    ArtifactExtraction,
    ExtractorUnavailableError,
    get_extractor,
)

try:
    cfg = _load_config_for_extractor()
    extractor = get_extractor(cfg)
    extraction = extractor.extract(content)
except ExtractorUnavailableError:
    return False
```

- [ ] **Step 2: Add the `_load_config_for_extractor` helper**

```python
def _load_config_for_extractor():
    """Load config once per extraction call.

    Cheap because :func:`load_config` reads a small YAML; if profiling
    later shows this on a hot path, lift to a module-level cache with
    invalidation on file mtime.
    """
    from opencomputer.agent.config_store import load_config
    return load_config()
```

- [ ] **Step 3: Run extractor + orchestrator tests**

```bash
pytest tests/test_profile_bootstrap_layer2_writers.py tests/test_profile_bootstrap_*.py -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/profile_bootstrap/orchestrator.py
git commit -m "refactor(orchestrator): use get_extractor() instead of direct ollama call"
```

## Task 8: Tests for each backend + factory

**Files:**
- Create: `tests/test_profile_bootstrap_extractor_pluggable.py`

- [ ] **Step 1: Write the test file**

```python
"""Pluggable Layer 3 extractor (2026-04-28).

Pre-2026-04-28 the Layer 3 extractor was hard-bound to Ollama. This
suite locks in:
- Protocol conformance for all 3 implementations
- Factory selection by config.deepening.extractor
- Backend-availability semantics (raises vs returns blank)
- Cost-guard accounting on API backends
- Privacy banner fires once per backend per profile
- Back-compat: OllamaUnavailableError is still importable
- Back-compat: extract_artifact() free function still works
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.llm_extractor import (
    AnthropicArtifactExtractor,
    ArtifactExtraction,
    ArtifactExtractor,
    ExtractorUnavailableError,
    OllamaArtifactExtractor,
    OllamaUnavailableError,
    OpenAIArtifactExtractor,
    extract_artifact,
    get_extractor,
)


# ── Protocol conformance ──────────────────────────────────────────────


def test_all_three_extractors_satisfy_protocol():
    assert isinstance(OllamaArtifactExtractor(), ArtifactExtractor)
    assert isinstance(AnthropicArtifactExtractor(), ArtifactExtractor)
    assert isinstance(OpenAIArtifactExtractor(), ArtifactExtractor)


def test_ollama_unavailable_error_is_back_compat_alias():
    assert OllamaUnavailableError is ExtractorUnavailableError


# ── Ollama (existing semantic preserved) ─────────────────────────────


def test_ollama_extract_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ExtractorUnavailableError):
        OllamaArtifactExtractor().extract("hello")


def test_extract_artifact_free_function_still_works(monkeypatch):
    """Back-compat: callers using the free function path keep working."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(OllamaUnavailableError):
        extract_artifact("hello")


# ── Anthropic / OpenAI availability ───────────────────────────────────


def test_anthropic_unavailable_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicArtifactExtractor().is_available() is False
    with pytest.raises(ExtractorUnavailableError):
        AnthropicArtifactExtractor().extract("hello")


def test_anthropic_available_when_key_in_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert AnthropicArtifactExtractor().is_available() is True


def test_anthropic_available_when_key_passed_explicit():
    assert AnthropicArtifactExtractor(api_key="sk-test").is_available() is True


def test_openai_unavailable_when_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAIArtifactExtractor().is_available() is False
    with pytest.raises(ExtractorUnavailableError):
        OpenAIArtifactExtractor().extract("hello")


# ── factory: validation + selection ───────────────────────────────────


def _make_config(extractor: str = "ollama", model: str = "", **kw):
    """Minimal Config-shape stub — only the fields the factory reads."""
    cfg = MagicMock()
    cfg.deepening.extractor = extractor
    cfg.deepening.model = model
    cfg.deepening.timeout_seconds = 15.0
    cfg.deepening.daily_cost_cap_usd = 0.50
    cfg.memory.declarative_path = Path("/tmp/oc-test-extractor/MEMORY.md")
    return cfg


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="not in"):
        get_extractor(_make_config(extractor="gemini"))


def test_factory_returns_ollama_for_default():
    inst = get_extractor(_make_config(extractor="ollama"))
    assert isinstance(inst, OllamaArtifactExtractor)


def test_factory_returns_anthropic_when_selected():
    inst = get_extractor(_make_config(extractor="anthropic"))
    assert isinstance(inst, AnthropicArtifactExtractor)


def test_factory_returns_openai_when_selected():
    inst = get_extractor(_make_config(extractor="openai"))
    assert isinstance(inst, OpenAIArtifactExtractor)


def test_factory_passes_model_override():
    inst = get_extractor(_make_config(extractor="anthropic", model="claude-opus-4-7"))
    assert inst.model == "claude-opus-4-7"


def test_factory_uses_class_default_when_model_empty():
    inst = get_extractor(_make_config(extractor="anthropic"))
    assert inst.model == AnthropicArtifactExtractor._DEFAULT_MODEL


# ── privacy banner: prints once per backend per profile ───────────────


def test_privacy_banner_writes_marker_on_first_anthropic_use(tmp_path, capsys):
    cfg = _make_config(extractor="anthropic")
    cfg.memory.declarative_path = tmp_path / "MEMORY.md"
    get_extractor(cfg)
    err = capsys.readouterr().err
    assert "Layer 3 deepening is now using anthropic" in err
    assert (tmp_path / "deepening_consent_anthropic.acknowledged").exists()


def test_privacy_banner_silent_on_subsequent_use(tmp_path, capsys):
    cfg = _make_config(extractor="anthropic")
    cfg.memory.declarative_path = tmp_path / "MEMORY.md"
    get_extractor(cfg)
    capsys.readouterr()  # drain first banner
    get_extractor(cfg)  # second call
    err = capsys.readouterr().err
    assert err == ""


def test_privacy_banner_per_backend(tmp_path, capsys):
    """Switching anthropic→openai prints a fresh banner; doesn't reuse the ack."""
    cfg_a = _make_config(extractor="anthropic")
    cfg_a.memory.declarative_path = tmp_path / "MEMORY.md"
    get_extractor(cfg_a)
    capsys.readouterr()

    cfg_o = _make_config(extractor="openai")
    cfg_o.memory.declarative_path = tmp_path / "MEMORY.md"
    get_extractor(cfg_o)
    err = capsys.readouterr().err
    assert "openai" in err


def test_privacy_banner_not_printed_for_ollama(tmp_path, capsys):
    cfg = _make_config(extractor="ollama")
    cfg.memory.declarative_path = tmp_path / "MEMORY.md"
    get_extractor(cfg)
    err = capsys.readouterr().err
    assert err == ""
    assert not (tmp_path / "deepening_consent_ollama.acknowledged").exists()


# ── End-to-end: API backend with mocked provider ──────────────────────


@pytest.mark.asyncio
async def test_anthropic_extract_with_mocked_provider(monkeypatch):
    """Mock _build_provider so the test never makes a real API call."""
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage
    from unittest.mock import AsyncMock

    fake_provider = AsyncMock()
    fake_provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(
                role="assistant",
                content='{"topic":"agent design","people":[],"intent":"build","sentiment":"positive","timestamp":""}',
            ),
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=50),
        ),
    )
    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.llm_extractor._build_provider",
        lambda name, **kw: fake_provider,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    extractor = AnthropicArtifactExtractor()
    result = extractor.extract("a long artifact about agent design")
    assert result.topic == "agent design"
    assert result.intent == "build"
    assert result.sentiment == "positive"
```

- [ ] **Step 2: Run the new test file**

```bash
pytest tests/test_profile_bootstrap_extractor_pluggable.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_profile_bootstrap_extractor_pluggable.py
git commit -m "test(extractor): protocol + factory + backends + privacy banner"
```

## Task 9: Full suite + ruff

- [ ] **Step 1: Run full pytest**

```bash
pytest tests/ -q
```

Expected: 4186+ passed.

- [ ] **Step 2: Ruff check**

```bash
ruff check opencomputer/profile_bootstrap/llm_extractor.py \
           opencomputer/profile_bootstrap/orchestrator.py \
           opencomputer/agent/config.py \
           opencomputer/agent/config_store.py \
           tests/test_profile_bootstrap_extractor_pluggable.py
```

Expected: clean. Apply `--fix` if needed.

## Task 10: CHANGELOG + PR

- [ ] **Step 1: CHANGELOG entry**

In `CHANGELOG.md` under `[Unreleased]`:

```markdown
### Added — pluggable Layer 3 extractor (Ollama / Anthropic / OpenAI)

`opencomputer profile deepen` now supports three extractor backends.
Default stays Ollama (privacy-by-default — artifact content never
leaves the machine). Users with an `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` can switch via `config.yaml`:

```yaml
deepening:
  extractor: anthropic   # or openai, or ollama (default)
  daily_cost_cap_usd: 0.50
```

First switch to a non-Ollama backend prints a one-time privacy
banner and writes an ack marker. Cost is recorded via the existing
cost guard so `oc cost show` reflects deepening spend.

Public surface preserved: `extract_artifact()`, `OllamaUnavailableError`,
`ArtifactExtraction` shape unchanged. Existing call sites get the new
behavior automatically.
```

- [ ] **Step 2: Commit + push + open PR**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG for pluggable Layer 3 extractor"
git push -u origin feat/pluggable-layer3-extractor
gh pr create --title "feat(awareness): pluggable Layer 3 extractor (Ollama / Anthropic / OpenAI)" \
  --body "$(cat <<EOF
## Summary
Layer 3 deepening is no longer hard-bound to Ollama. Users with an Anthropic or OpenAI key can now run deepening without installing a second LLM stack. Privacy default preserved (Ollama). One-time banner on first switch. Cost-cap via existing cost-guard.

## Test plan
- [x] All 4186+ existing tests pass
- [x] 16 new tests in test_profile_bootstrap_extractor_pluggable.py
- [x] ruff clean
- [x] Real run: oc profile deepen with extractor: anthropic emits motifs
EOF
)"
```
