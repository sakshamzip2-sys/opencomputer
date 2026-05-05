"""Centralized LLM-call observability.

Single sink: record_llm_call(event) appends a JSONL line to
~/.opencomputer/<profile>/llm_events.jsonl. Rotates at 100MB,
keeping at most MAX_BAK_FILES rotated copies.

Wired in:
  - extensions/anthropic-provider/provider.py (in complete() and stream_complete())
  - extensions/openai-provider/provider.py (same)
  - opencomputer/evals/runner.py (eval_grader site tag)

Single source of truth: providers emit events; agent loop and eval harness
pass `site` when calling the provider but do not call record_llm_call themselves.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LOG_ROTATE_MB = 100
MAX_BAK_FILES = 5

_logger = logging.getLogger("opencomputer.inference.observability")

# Out-of-band subscribers (e.g. langfuse plugin). Each subscriber gets
# called fire-and-forget on every event; exceptions are logged at WARN
# but never propagate — telemetry must not break the agent loop.
_subscribers: list[Callable[[LLMCallEvent], None]] = []


def register_subscriber(callback: Callable[[LLMCallEvent], None]) -> None:
    """Register an out-of-band subscriber for every recorded LLM call.

    Plugins (e.g. langfuse observability bridge) call this at register
    time. Idempotent — adding the same callback twice is a no-op.
    """
    if callback not in _subscribers:
        _subscribers.append(callback)


def unregister_subscriber(callback: Callable[[LLMCallEvent], None]) -> None:
    """Remove a previously-registered subscriber. Tests use this."""
    if callback in _subscribers:
        _subscribers.remove(callback)


@dataclass(frozen=True)
class LLMCallEvent:
    ts: datetime
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    latency_ms: int
    cost_usd: float | None
    site: str | None


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    return Path.home() / ".opencomputer" / os.environ.get("OPENCOMPUTER_PROFILE", "default")


def _log_path() -> Path:
    home = _profile_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "llm_events.jsonl"


def _prune_bak_files(active: Path) -> None:
    """Keep only the most recent MAX_BAK_FILES rotated logs."""
    pattern = f"{active.stem}.jsonl.*.bak"
    bak_files = sorted(
        active.parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for old in bak_files[MAX_BAK_FILES:]:
        old.unlink()


def _maybe_rotate(path: Path) -> None:
    if not path.exists():
        return
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb < LOG_ROTATE_MB:
        return
    rotated = path.with_suffix(f".jsonl.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
    path.rename(rotated)
    _prune_bak_files(path)


def record_llm_call(event: LLMCallEvent) -> None:
    """Append one event to the JSONL log + fan out to subscribers."""
    path = _log_path()
    _maybe_rotate(path)
    with path.open("a") as f:
        d = asdict(event)
        d["ts"] = event.ts.isoformat()
        f.write(json.dumps(d) + "\n")

    for sub in list(_subscribers):
        try:
            sub(event)
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the loop
            _logger.warning("LLMCallEvent subscriber %r raised: %s", sub, exc)
