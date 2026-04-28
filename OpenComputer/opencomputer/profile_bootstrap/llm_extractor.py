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
import time
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


# ─── Smart fallback (2026-04-28) ─────────────────────────────────────
#
# When the user is on the default ``extractor: ollama`` but doesn't have
# Ollama installed AND has a cloud API key in env, offer to switch.
# This bridges the gap between "privacy-by-default" and "your existing
# key already paid the privacy cost — no point installing a second LLM
# stack". Strict gates so the prompt only fires when:
#
#   1. stderr is a TTY (interactive run; CI/tests/daemons skip)
#   2. extractor is the ollama default (explicit picks aren't second-guessed)
#   3. Ollama isn't on PATH (if it is, no fallback needed)
#   4. ANTHROPIC_API_KEY or OPENAI_API_KEY is set (we have something to offer)
#   5. The marker file at ``~/.opencomputer/<profile>/extractor_setup.json``
#      doesn't exist (we don't pester after answer)


def _smart_fallback_candidate() -> tuple[str, str] | None:
    """Return ``(backend_name, env_var)`` for the strongest candidate, or None.

    Anthropic-preferred ordering — picked because the user's already-running
    chat path (in this codebase) defaults to Anthropic, so the same key is
    most likely already exercised. OpenAI is the backup so ``OPENAI_API_KEY``
    holders aren't excluded.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return ("anthropic", "ANTHROPIC_API_KEY")
    if os.getenv("OPENAI_API_KEY"):
        return ("openai", "OPENAI_API_KEY")
    return None


def _maybe_offer_extractor_setup() -> str | None:
    """Run the one-time interactive setup if all gates pass.

    Returns the chosen backend name on accept (and persists to
    config.yaml + writes the per-backend privacy ack so the standard
    banner doesn't fire on the next call). Returns ``None`` on any
    other path — non-interactive, marker exists, no candidate, user
    declined. Caller must handle ``None`` (typically: fall through to
    Ollama which will then raise ``ExtractorUnavailableError``).
    """
    import sys

    if not sys.stderr.isatty():
        return None  # non-interactive — never prompt

    # Marker check up front: if we've already asked, never ask again.
    try:
        from opencomputer.agent.config import _home
        profile_home = _home()
    except Exception:  # noqa: BLE001
        return None
    marker = profile_home / "extractor_setup.json"
    if marker.exists():
        return None

    candidate = _smart_fallback_candidate()
    if candidate is None:
        return None

    backend, env_var = candidate

    sys.stderr.write(
        f"\n"
        f"Ollama is not installed, but {env_var} is set in your environment.\n"
        f"\n"
        f"Layer 3 deepening (background extraction of topic / intent / people\n"
        f"from your recent files + browser pages) needs an LLM backend.\n"
        f"\n"
        f"  Option A — install Ollama:    brew install ollama && ollama pull qwen2.5:3b\n"
        f"             Free, fully local, content never leaves your machine.\n"
        f"\n"
        f"  Option B — use {backend}:           ~$0.001 per artifact (~$0.05–0.20 per pass)\n"
        f"             Artifact content (file bodies, page text) sent to {backend}.\n"
        f"\n"
        f"Use {backend} for now? You can switch later by editing config.yaml.\n"
        f"[y/N]: "
    )
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    # Persist the answer either way so a re-run doesn't re-prompt.
    try:
        profile_home.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "answered_at": time.time(),
            "answer": answer,
            "backend_offered": backend,
        }))
    except OSError:
        pass

    if answer not in ("y", "yes"):
        sys.stderr.write(
            "OK — keeping the Ollama default. Install Ollama later to enable\n"
            "Layer 3 deepening, or set deepening.extractor in config.yaml.\n\n"
        )
        return None

    # Persist to config.yaml and pre-ack the privacy banner (the prompt
    # already disclosed the privacy posture; printing the banner on the
    # next call would just double-tell them the same thing).
    try:
        from dataclasses import replace as _replace

        from opencomputer.agent.config_store import load_config, save_config

        loaded = load_config()
        new_cfg = _replace(
            loaded, deepening=_replace(loaded.deepening, extractor=backend),
        )
        save_config(new_cfg)
        sys.stderr.write(
            f"✓ Saved deepening.extractor: {backend} to config.yaml\n\n"
        )
    except Exception:  # noqa: BLE001 — never fail extraction over a config write
        sys.stderr.write(
            "! Could not persist to config.yaml — using "
            f"{backend} for this run only.\n\n"
        )

    try:
        ack = profile_home / f"deepening_consent_{backend}.acknowledged"
        ack.write_text("acknowledged via smart fallback\n")
    except OSError:
        pass

    return backend


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

    # Smart fallback: user is on the default ``ollama`` but Ollama
    # isn't installed AND a cloud key is set in env → offer to switch
    # rather than silently failing. Strict gates inside
    # ``_maybe_offer_extractor_setup`` (TTY-only, marker-once, etc.)
    # mean this is a no-op for non-interactive paths and after the
    # first answer.
    if name == "ollama" and not OllamaArtifactExtractor().is_available():
        chosen = _maybe_offer_extractor_setup()
        if chosen:
            name = chosen  # banner already pre-acked inside the offer

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
