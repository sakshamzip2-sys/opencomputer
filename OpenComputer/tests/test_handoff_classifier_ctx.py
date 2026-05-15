"""Tests for §1.2 — closing the hardcoded classifier-context bug.

Before this fix, ``run_auto_swap_pipeline`` passed
``time_of_day_hour=12`` and ``recent_file_paths=()`` regardless of
actual time / activity. The classifier
(``opencomputer/awareness/personas/classifier.py:161-172``) uses both
signals — extension-frequency for coding/learning, time-of-day for
evening/morning routing. Hardcoded values made these heuristics dead.

These tests assert: real wall-clock hour reaches the classifier and
file paths are extracted from recent assistant tool_use blocks.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_extract_paths_empty_messages_returns_empty() -> None:
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    assert _extract_recent_file_paths([]) == ()
    assert _extract_recent_file_paths(()) == ()


def test_extract_paths_ignores_user_role() -> None:
    """Only assistant turns carry tool_use blocks worth scanning."""
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "user",
            "content": [{"type": "tool_use", "input": {"file_path": "/x.py"}}],
        }
    ]
    assert _extract_recent_file_paths(msgs) == ()


def test_extract_paths_picks_up_file_path_key() -> None:
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "/repo/a.py", "old_string": "x"},
                },
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/repo/b.py"},
                },
            ],
        }
    ]
    paths = _extract_recent_file_paths(msgs)
    assert paths == ("/repo/a.py", "/repo/b.py")


def test_extract_paths_dedupes_preserving_order() -> None:
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "input": {"file_path": "/x.py"}},
                {"type": "tool_use", "input": {"file_path": "/y.py"}},
                {"type": "tool_use", "input": {"file_path": "/x.py"}},  # dup
            ],
        }
    ]
    assert _extract_recent_file_paths(msgs) == ("/x.py", "/y.py")


def test_extract_paths_accepts_alternate_keys() -> None:
    """coding-harness tools may use ``path`` or ``absolute_path``."""
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "input": {"path": "/p1"}},
                {"type": "tool_use", "input": {"absolute_path": "/p2"}},
                {"type": "tool_use", "input": {"notebook_path": "/p3.ipynb"}},
                {"type": "tool_use", "input": {"filePath": "/p4"}},  # camelCase
            ],
        }
    ]
    paths = _extract_recent_file_paths(msgs)
    assert paths == ("/p1", "/p2", "/p3.ipynb", "/p4")


def test_extract_paths_ignores_non_string_values() -> None:
    """Adversarial / malformed input → graceful skip."""
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "input": {"file_path": None}},
                {"type": "tool_use", "input": {"file_path": 42}},
                {"type": "tool_use", "input": {"file_path": ""}},
                {"type": "tool_use", "input": {"file_path": "   "}},
                {"type": "tool_use", "input": {"file_path": "/good.py"}},
            ],
        }
    ]
    assert _extract_recent_file_paths(msgs) == ("/good.py",)


def test_extract_paths_handles_namespace_objects() -> None:
    """Some callers pass attribute-style objects, not dicts."""
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msg = SimpleNamespace(
        role="assistant",
        content=[{"type": "tool_use", "input": {"file_path": "/n.py"}}],
    )
    assert _extract_recent_file_paths([msg]) == ("/n.py",)


def test_extract_paths_caps_at_max() -> None:
    """Hard cap prevents pathological context from blowing the classifier."""
    from opencomputer.agent.handoff.orchestrator import (
        _MAX_RECENT_FILE_PATHS,
        _extract_recent_file_paths,
    )
    msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "input": {"file_path": f"/p{i}.py"}}
            for i in range(_MAX_RECENT_FILE_PATHS + 20)
        ],
    }
    paths = _extract_recent_file_paths([msg])
    assert len(paths) == _MAX_RECENT_FILE_PATHS


def test_extract_paths_ignores_string_content() -> None:
    """Plain text assistant turns have content: str — no blocks to scan."""
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [{"role": "assistant", "content": "just text"}]
    assert _extract_recent_file_paths(msgs) == ()


def test_extract_paths_ignores_non_tool_use_blocks() -> None:
    from opencomputer.agent.handoff.orchestrator import _extract_recent_file_paths
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "/file_path/in/text.py"},  # not a tool_use
                {"type": "tool_result", "content": "/result.py"},  # not tool_use
            ],
        }
    ]
    assert _extract_recent_file_paths(msgs) == ()


def test_classifier_ctx_uses_real_hour() -> None:
    """The orchestrator must pass datetime.now().hour, not 12."""
    import opencomputer.agent.handoff.orchestrator as orch_mod
    with open(orch_mod.__file__) as f:
        src = f.read()
    # The literal "12" must be replaced; either datetime.now().hour or
    # an equivalent expression is acceptable.
    assert "time_of_day_hour=12," not in src
    assert (
        "time_of_day_hour=_dt.datetime.now().hour" in src
        or "datetime.now().hour" in src
    )


def test_classifier_ctx_uses_real_file_paths() -> None:
    """The orchestrator must pass extracted recent_file_paths, not ()."""
    import opencomputer.agent.handoff.orchestrator as orch_mod
    with open(orch_mod.__file__) as f:
        src = f.read()
    assert "recent_file_paths=()," not in src
    assert "_extract_recent_file_paths(recent_messages)" in src
