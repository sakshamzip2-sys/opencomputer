"""LLM extractor — pluggable backend for Layer 2/3 artifact extraction.

Used by Layer 2 (Recent Context Scan) and Layer 3 (Background Deepening)
to turn unstructured artifacts (file content, mail bodies, git commit
messages) into structured :class:`ArtifactExtraction` records that
flow into the F2 SignalEvent bus.

Backends (2026-04-28):

* :class:`OllamaArtifactExtractor` — local subprocess. Default. Free.
  Content never leaves the machine. Requires ``ollama`` on PATH.
* :class:`AnthropicArtifactExtractor` — Anthropic SDK. Requires
  ``ANTHROPIC_API_KEY``. Cost-tracked via the existing cost guard.
* :class:`OpenAIArtifactExtractor` — OpenAI SDK. Requires
  ``OPENAI_API_KEY``. Cost-tracked.

The factory :func:`get_extractor` picks one based on
``config.deepening.extractor``. First switch to a non-Ollama backend
prints a one-time privacy banner; an ack marker prevents repeats.

Backwards compatibility (load-bearing for existing call sites):

* :func:`extract_artifact` / :func:`is_ollama_available` keep their
  signatures and dispatch to the Ollama backend.
* :class:`OllamaUnavailableError` stays importable as an alias of the
  more general :class:`ExtractorUnavailableError`.
* :data:`_DEFAULT_MODEL` module-level constant preserved for any
  external code that imported it directly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_DEFAULT_MODEL = "llama3.2:3b"  # back-compat — Ollama default model
_EXTRACTION_PROMPT = """You are a JSON extractor. Given the artifact below, return ONE JSON object with these keys ONLY:
- topic: 1-3 word topic
- people: list of person names mentioned (empty list if none)
- intent: one sentence summarizing what the user might be trying to do (empty string if unclear)
- sentiment: one of "positive" / "neutral" / "negative" / "unknown"
- timestamp: ISO 8601 if present in artifact, else empty string

Return ONLY the JSON. No prose, no code block.

Artifact:
{artifact}
"""


# ─── Errors ──────────────────────────────────────────────────────────


class ExtractorUnavailableError(RuntimeError):
    """The chosen backend is missing — binary absent, API key unset, etc."""


# Back-compat: pre-2026-04-28 callers caught ``OllamaUnavailableError``.
# Keeping the alias means ``except OllamaUnavailableError:`` still
# matches the new ``ExtractorUnavailableError`` raised by any backend.
OllamaUnavailableError = ExtractorUnavailableError


# ─── Output dataclass (frozen — public contract) ─────────────────────


@dataclass(frozen=True, slots=True)
class ArtifactExtraction:
    """Structured output of one LLM extraction call. All fields safe-default."""

    topic: str = ""
    people: tuple[str, ...] = ()
    intent: str = ""
    sentiment: str = "unknown"
    timestamp: str = ""


# ─── Protocol ────────────────────────────────────────────────────────


@runtime_checkable
class ArtifactExtractor(Protocol):
    """Pluggable Layer 3 backend.

    Implementations are stateless (each call independent) and
    deterministic about availability — ``is_available()`` returns the
    same answer until external state (env, install) changes.
    """

    def is_available(self) -> bool: ...

    def extract(self, content: str) -> ArtifactExtraction: ...


# ─── Approximate per-Mtok pricing for cost-guard accounting ──────────
# Rough 2026-04 pricing in USD per 1M tokens. Conservative — actual
# bills should match within a few percent. Update when providers move.

_PRICING_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    # (provider, model_prefix) -> (input_$/Mtok, output_$/Mtok)
    ("anthropic", "claude-haiku-4-5"): (0.80, 4.00),
    ("anthropic", "claude-haiku-3-5"): (0.80, 4.00),
    ("anthropic", "claude-sonnet-4"): (3.00, 15.00),
    ("anthropic", "claude-opus-4"): (15.00, 75.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4.1-mini"): (0.40, 1.60),
}


def _estimate_cost_usd(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> float:
    """Best-effort USD estimate. Unknown model → assume cheap-tier rates
    so cost-guard still records a non-zero value (better than silent zero)."""
    fallback = (0.50, 2.00)  # conservative cheap-tier guess
    for (p, prefix), rates in _PRICING_PER_MTOK.items():
        if p == provider and model.startswith(prefix):
            in_rate, out_rate = rates
            break
    else:
        in_rate, out_rate = fallback
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _record_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """Append a cost-guard usage row. Best-effort — never blocks extraction."""
    try:
        from opencomputer.cost_guard.guard import get_default_guard
        cost = _estimate_cost_usd(provider, model, input_tokens, output_tokens)
        if cost > 0:
            get_default_guard().record_usage(
                provider, cost_usd=cost, operation="deepening_extraction",
            )
    except Exception:  # noqa: BLE001 — cost recording is informational
        pass


# ─── Ollama backend ──────────────────────────────────────────────────


class OllamaArtifactExtractor:
    """Local-first extractor — Ollama subprocess. Zero cost, all
    content stays on the machine."""

    _DEFAULT_MODEL = _DEFAULT_MODEL

    def __init__(self, *, model: str = "", timeout_seconds: float = 15.0) -> None:
        self.model = model or self._DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> bool:
        # Delegate to the module-level helper so tests that patch
        # ``llm_extractor.is_ollama_available`` (pre-2026-04-28 style)
        # still intercept this path through the class.
        return is_ollama_available()

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


# ─── Anthropic backend ───────────────────────────────────────────────


class AnthropicArtifactExtractor:
    """Anthropic SDK extractor. Sends artifact content to Anthropic.

    Uses the official ``anthropic`` Python SDK directly (not the agent's
    ``BaseProvider`` plumbing) because we only need a single sync
    completion call — no streaming, no tool-use, no event-loop wiring.
    Honors ``ANTHROPIC_BASE_URL`` for proxy / Claude Router setups.
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
        return bool(self._api_key or os.getenv("ANTHROPIC_API_KEY"))

    def extract(self, content: str) -> ArtifactExtraction:
        if not self.is_available():
            raise ExtractorUnavailableError(
                "anthropic extractor needs ANTHROPIC_API_KEY in env"
            )
        artifact = content[:4000]
        prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError:
            raise ExtractorUnavailableError(
                "anthropic SDK not installed; run 'pip install anthropic'"
            ) from None

        kwargs: dict[str, Any] = {
            "api_key": self._api_key or os.getenv("ANTHROPIC_API_KEY"),
            "timeout": self.timeout_seconds,
        }
        # Proxy / Claude Router support — Saksham's setup uses this.
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url

        try:
            client = anthropic.Anthropic(**kwargs)
            response = client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 — match existing blank-on-failure
            return ArtifactExtraction()

        try:
            usage = response.usage
            _record_cost(
                "anthropic", self.model,
                int(getattr(usage, "input_tokens", 0)),
                int(getattr(usage, "output_tokens", 0)),
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            text = response.content[0].text  # type: ignore[union-attr]
        except (AttributeError, IndexError, TypeError):
            return ArtifactExtraction()
        return _parse_extraction(text.strip())


# ─── OpenAI backend ──────────────────────────────────────────────────


class OpenAIArtifactExtractor:
    """OpenAI SDK extractor. Uses ``gpt-4o-mini`` by default for the
    price/quality sweet spot on JSON extraction. Override via
    ``DeepeningConfig.model``."""

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
        return bool(self._api_key or os.getenv("OPENAI_API_KEY"))

    def extract(self, content: str) -> ArtifactExtraction:
        if not self.is_available():
            raise ExtractorUnavailableError(
                "openai extractor needs OPENAI_API_KEY in env"
            )
        artifact = content[:4000]
        prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError:
            raise ExtractorUnavailableError(
                "openai SDK not installed; run 'pip install openai'"
            ) from None

        kwargs: dict[str, Any] = {
            "api_key": self._api_key or os.getenv("OPENAI_API_KEY"),
            "timeout": self.timeout_seconds,
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url

        try:
            client = openai.OpenAI(**kwargs)
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001
            return ArtifactExtraction()

        try:
            usage = response.usage
            _record_cost(
                "openai", self.model,
                int(getattr(usage, "prompt_tokens", 0)),
                int(getattr(usage, "completion_tokens", 0)),
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError):
            return ArtifactExtraction()
        return _parse_extraction(text.strip())


# ─── Factory ─────────────────────────────────────────────────────────


_KNOWN_EXTRACTORS = ("ollama", "anthropic", "openai")


def get_extractor(config: Any) -> ArtifactExtractor:
    """Return the configured extractor instance.

    ``config`` is the loaded :class:`opencomputer.agent.config.Config`.
    Raises ``ValueError`` if ``deepening.extractor`` is not in
    :data:`_KNOWN_EXTRACTORS`.

    Prints a one-time privacy banner (to stderr) the first time the
    user picks a non-Ollama backend on this profile. The banner is
    suppressed when stderr is not a tty (so log files stay clean).
    """
    cfg = config.deepening
    name = (cfg.extractor or "ollama").lower()
    if name not in _KNOWN_EXTRACTORS:
        raise ValueError(
            f"deepening.extractor={name!r} not in {_KNOWN_EXTRACTORS}"
        )

    if name != "ollama":
        _maybe_print_privacy_banner(name)

    if name == "ollama":
        return OllamaArtifactExtractor(
            model=cfg.model, timeout_seconds=cfg.timeout_seconds,
        )
    if name == "anthropic":
        return AnthropicArtifactExtractor(
            model=cfg.model, timeout_seconds=cfg.timeout_seconds,
        )
    return OpenAIArtifactExtractor(
        model=cfg.model, timeout_seconds=cfg.timeout_seconds,
    )


def _maybe_print_privacy_banner(backend: str) -> None:
    """One-time privacy notice when switching to a cloud backend.

    Marker file lives directly in the active profile dir
    (``~/.opencomputer/<profile>/``) so it survives across runs but
    resets if the user copies a profile.
    """
    try:
        from opencomputer.agent.config import _home
        profile_home = _home()
    except Exception:  # noqa: BLE001
        return  # config malformed — skip banner, don't crash extraction

    marker = profile_home / f"deepening_consent_{backend}.acknowledged"
    if marker.exists():
        return
    sys.stderr.write(
        f"\nⓘ  Layer 3 deepening is now using {backend}.\n"
        f"   Artifact content (file bodies, browser pages) will be sent\n"
        f"   to {backend} for extraction. To stay local, set\n"
        f"   `deepening.extractor: ollama` in config.yaml.\n\n"
    )
    try:
        profile_home.mkdir(parents=True, exist_ok=True)
        marker.write_text("acknowledged\n")
    except OSError:
        pass  # ack marker is best-effort


# ─── Back-compat free functions ──────────────────────────────────────


def is_ollama_available() -> bool:
    """Cheap probe — only checks that the ``ollama`` binary is on PATH."""
    return shutil.which("ollama") is not None


def extract_artifact(
    content: str,
    *,
    model: str = _DEFAULT_MODEL,
    timeout_seconds: float = 15.0,
) -> ArtifactExtraction:
    """Run one extraction via Ollama. Back-compat free function — new
    callers should resolve via :func:`get_extractor` to honor the
    user's ``deepening.extractor`` choice. This shim always uses the
    Ollama backend for callers that imported the function directly.
    """
    return OllamaArtifactExtractor(
        model=model, timeout_seconds=timeout_seconds,
    ).extract(content)


# ─── Internal: JSON parser ───────────────────────────────────────────


def _parse_extraction(raw: str) -> ArtifactExtraction:
    """Best-effort JSON parse. Returns blank extraction on any failure."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ArtifactExtraction()
    if not isinstance(data, dict):
        return ArtifactExtraction()
    return ArtifactExtraction(
        topic=str(data.get("topic", ""))[:128],
        people=tuple(str(p)[:64] for p in data.get("people", []) if isinstance(p, str))[:10],
        intent=str(data.get("intent", ""))[:512],
        sentiment=str(data.get("sentiment", "unknown")).lower(),
        timestamp=str(data.get("timestamp", ""))[:32],
    )


# Suppress unused-import warning on Path — used by callers via re-export.
_ = Path
