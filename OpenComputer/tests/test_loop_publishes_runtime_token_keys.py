"""AgentLoop publishes the keys that drive the TUI bar + ``/context``.

Before this fix, ``loop.py`` only set ``self._last_input_tokens`` as an
instance attribute. The slash command and status-line bar read
``runtime.custom["last_input_tokens"]`` — a key nothing wrote — so the
"prefer current-turn input" branch was permanently dead, silently
falling through to the cumulative ``session_tokens_in`` (the 10x-
inflation footgun the deep-dive flagged).

Two source-level guards are enough to keep that wire alive:

  1. ``loop.py`` writes ``runtime.custom["last_input_tokens"]`` at the
     same site that updates ``self._last_input_tokens``. A regression
     that drops the runtime write but keeps the instance write would
     silently re-introduce the bug; this test fails the build instead.

  2. ``loop.py`` publishes the effective
     ``compaction_threshold_ratio`` so ``/context`` displays the user's
     customised value rather than ``CompactionConfig`` 's default.

Both are source-level grep tests — the same shape
``test_loop_compaction_increments_counter.py`` uses to keep its two
``_record_compaction()`` call sites honest. The integration-level
behavior is covered by ``test_compaction_runtime_resolvers.py``.
"""

from __future__ import annotations

from pathlib import Path

_LOOP_PATH = (
    Path(__file__).parent.parent / "opencomputer" / "agent" / "loop.py"
)


def _read_loop_source() -> str:
    return _LOOP_PATH.read_text(encoding="utf-8")


def test_loop_publishes_last_input_tokens_to_runtime_custom() -> None:
    """The loop must mirror ``self._last_input_tokens`` into
    ``runtime.custom["last_input_tokens"]`` so ``/context`` and the
    TUI bar can read it. Source-level grep guards against a refactor
    that drops the runtime write while keeping the instance attribute
    update.
    """
    src = _read_loop_source()
    # Defensive: don't pin the exact whitespace / int-coerce style.
    # The test cares that *some* assignment to
    # ``runtime.custom["last_input_tokens"]`` exists inside loop.py.
    assert '"last_input_tokens"' in src, (
        'loop.py must publish runtime.custom["last_input_tokens"] '
        "so /context and the status-line bar can read it"
    )
    # And the assignment must be on a runtime.custom dict. We accept
    # either the instance-prefixed (``self._runtime.custom``) or the
    # local-alias (``runtime.custom``) form — the wire-up just needs
    # to land on a runtime dict.
    expected_patterns = (
        'self._runtime.custom["last_input_tokens"]',
        'runtime.custom["last_input_tokens"]',
    )
    assert any(p in src for p in expected_patterns), (
        "loop.py must assign last_input_tokens onto a runtime.custom "
        f"dict (looked for {expected_patterns!r})"
    )


def test_loop_publishes_compaction_threshold_ratio_to_runtime_custom() -> None:
    """The loop must publish the engine's effective threshold ratio so
    /context can render the user's customised value rather than the
    hand-typed default.
    """
    src = _read_loop_source()
    assert '"compaction_threshold_ratio"' in src, (
        'loop.py must publish '
        'runtime.custom["compaction_threshold_ratio"] so /context can '
        "display the effective ratio (default 0.8 or a "
        "config.yaml override) consistently with the engine"
    )


def test_loop_publishes_last_input_alongside_session_tokens() -> None:
    """Locality check: the ``last_input_tokens`` runtime write should
    sit close to (within ~50 lines of) the ``session_tokens_in`` write,
    so the cumulative + current pair stay in lockstep across refactors.
    """
    src = _read_loop_source()
    lines = src.splitlines()
    last_idx = next(
        (i for i, line in enumerate(lines) if '"last_input_tokens"' in line),
        None,
    )
    session_idx = next(
        (i for i, line in enumerate(lines) if '"session_tokens_in"' in line),
        None,
    )
    assert last_idx is not None and session_idx is not None
    assert abs(last_idx - session_idx) <= 50, (
        "last_input_tokens and session_tokens_in publishing should "
        "stay co-located so a refactor that touches one prompts a "
        "review of the other (found "
        f"last_input_tokens@{last_idx + 1}, "
        f"session_tokens_in@{session_idx + 1})"
    )
