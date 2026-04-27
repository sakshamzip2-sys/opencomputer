"""Tool result persistence — preserves large outputs instead of truncating.

Ported from ``sources/hermes-agent-2026.4.23/tools/tool_result_storage.py``
for OpenComputer (TS-T2) with two adaptations:

1. The ``env=`` sandbox abstraction (Hermes runs in Docker/SSH/Modal/Daytona
   backends) is dropped. OC runs locally — we write directly via
   :py:meth:`pathlib.Path.write_text`. The storage directory is
   ``<profile_home>/tool_result_storage/`` resolved through
   :func:`opencomputer.agent.config._home`.

2. The heredoc-based ``_write_to_sandbox`` helper is replaced with a simple
   :py:meth:`pathlib.Path.write_text` (UTF-8, ``errors="replace"``) call.

Defense against context-window overflow operates at three levels:

1. **Per-tool output cap** (inside each tool): Tools like ``Grep`` /
   ``search_files`` pre-truncate their own output before returning. This is
   the first line of defense and the only one the tool author controls.

2. **Per-result persistence** (:func:`maybe_persist_tool_result`): After a
   tool returns, if its output exceeds the tool's registered threshold
   (see :class:`opencomputer.agent.budget_config.BudgetConfig`), the full
   output is written to ``<profile_home>/tool_result_storage/{tool_use_id}.txt``.
   The in-context content is replaced with a preview + file path reference.
   The model can use the ``Read`` tool to access the full output.

3. **Per-turn aggregate budget** (:func:`enforce_turn_budget`): After all
   tool results in a single assistant turn are collected, if the total
   exceeds ``turn_budget`` (200K by default), the largest non-persisted
   results are spilled to disk until the aggregate is under budget. This
   catches cases where many medium-sized results combine to overflow context.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opencomputer.agent.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
    DEFAULT_BUDGET,
)
from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# Sentinel tool name used by enforce_turn_budget when re-routing through
# maybe_persist_tool_result with an explicit ``threshold=0`` override (so
# pinned-tool guards don't accidentally short-circuit budget enforcement).
_BUDGET_TOOL_NAME = "__budget_enforcement__"


def _resolve_storage_dir() -> Path:
    """Return ``<profile_home>/tool_result_storage/``.

    Resolved on every call so test fixtures that monkeypatch
    ``OPENCOMPUTER_HOME`` (which ``_home()`` honours) take effect without
    needing to reset module-level state.
    """
    return _home() / "tool_result_storage"


def _write_local(content: str, path: Path) -> bool:
    """Write ``content`` to ``path``. Returns True on success.

    Local-filesystem replacement for Hermes's heredoc-based sandbox write.
    UTF-8 with ``errors="replace"`` keeps us robust against arbitrary
    binary-ish output (Bash that prints non-UTF-8 bytes, for example).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", errors="replace")
        return True
    except OSError as exc:
        logger.warning("Local write failed for %s: %s", path, exc)
        return False


def generate_preview(
    content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS
) -> tuple[str, bool]:
    """Truncate at last newline within ``max_chars``. Returns ``(preview, has_more)``."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[: last_nl + 1]
    return truncated, True


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """Build the ``<persisted-output>`` replacement block."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the Read tool with offset and limit to access specific sections of this output.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
) -> str:
    """Layer 2: persist oversized result to disk, return preview + path.

    Writes the full output to ``<profile_home>/tool_result_storage/{tool_use_id}.txt``.
    Falls back to inline truncation if the local write fails.

    Args:
        content: Raw tool result string.
        tool_name: Name of the tool (used for threshold lookup).
        tool_use_id: Unique ID for this tool call (used as filename).
        config: BudgetConfig controlling thresholds and preview size.
        threshold: Explicit override; takes precedence over config resolution.

    Returns:
        Original content if small, or the ``<persisted-output>`` replacement.
    """
    effective_threshold = (
        threshold if threshold is not None else config.resolve_threshold(tool_name)
    )

    # ``inf`` threshold means "never persist" — used to break recursion on
    # tools like ``Read`` whose entire purpose is to surface persisted files.
    if effective_threshold == float("inf"):
        return content

    if len(content) <= effective_threshold:
        return content

    storage_dir = _resolve_storage_dir()
    file_path = storage_dir / f"{tool_use_id}.txt"
    preview, has_more = generate_preview(content, max_chars=config.preview_size)

    if _write_local(content, file_path):
        logger.info(
            "Persisted large tool result: %s (%s, %d chars -> %s)",
            tool_name,
            tool_use_id,
            len(content),
            file_path,
        )
        return _build_persisted_message(
            preview, has_more, len(content), str(file_path)
        )

    # Local write failed — fall back to inline truncation so the model still
    # sees a head-snippet rather than a black hole.
    logger.info(
        "Inline-truncating large tool result: %s (%d chars, local write failed)",
        tool_name,
        len(content),
    )
    return (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to disk.]"
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    config: BudgetConfig = DEFAULT_BUDGET,
) -> list[dict]:
    """Layer 3: enforce aggregate budget across all tool results in a turn.

    If total chars exceed ``config.turn_budget``, persist the largest
    non-persisted results first (via local write) until under budget.
    Already-persisted results (containing :data:`PERSISTED_OUTPUT_TAG`) are
    skipped — this also gives idempotency when the same list is enforced
    twice.

    Mutates the list in-place and returns it. Each dict is expected to have
    ``content`` (str) and ``tool_call_id`` (str) keys.
    """
    candidates: list[tuple[int, int]] = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "") or ""
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")

        replacement = maybe_persist_tool_result(
            content=content,
            tool_name=_BUDGET_TOOL_NAME,
            tool_use_id=tool_use_id,
            config=config,
            threshold=0,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id,
                size,
            )

    return tool_messages


__all__ = [
    "PERSISTED_OUTPUT_TAG",
    "PERSISTED_OUTPUT_CLOSING_TAG",
    "enforce_turn_budget",
    "generate_preview",
    "maybe_persist_tool_result",
]
