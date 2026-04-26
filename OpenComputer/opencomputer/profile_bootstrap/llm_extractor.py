"""LLM extractor — local-first via Ollama subprocess.

Used by Layer 2 (Recent Context Scan) and Layer 3 (Background Deepening)
to turn unstructured artifacts (file content, mail bodies, git commit
messages) into structured :class:`ArtifactExtraction` records that
flow into the F2 SignalEvent bus.

If Ollama is not installed, :func:`is_ollama_available` returns False
and :func:`extract_artifact` raises :class:`OllamaUnavailable`. Callers
must handle that — deepening proceeds with whatever extraction it can.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


_DEFAULT_MODEL = "llama3.2:3b"
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


class OllamaUnavailable(RuntimeError):
    """Raised when Ollama isn't on PATH."""


@dataclass(frozen=True, slots=True)
class ArtifactExtraction:
    """Structured output of one LLM extraction call. All fields safe-default."""

    topic: str = ""
    people: tuple[str, ...] = ()
    intent: str = ""
    sentiment: str = "unknown"
    timestamp: str = ""


def is_ollama_available() -> bool:
    """Cheap probe — only checks that the binary is on PATH."""
    return shutil.which("ollama") is not None


def extract_artifact(
    content: str,
    *,
    model: str = _DEFAULT_MODEL,
    timeout_seconds: float = 15.0,
) -> ArtifactExtraction:
    """Run one extraction. Raises :class:`OllamaUnavailable` if not available.

    Returns blank :class:`ArtifactExtraction` on malformed JSON, timeout, or
    nonzero exit. Truncates content to ~4000 chars for context budget.
    """
    if not is_ollama_available():
        raise OllamaUnavailable("ollama not on PATH; install via 'brew install ollama'")
    artifact = content[:4000]
    prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ArtifactExtraction()
    if result.returncode != 0:
        return ArtifactExtraction()
    return _parse_extraction(result.stdout.strip())


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
