"""TS-T2 — Tool result spillover tests (Layers 2 + 3)."""

from opencomputer.agent.budget_config import BudgetConfig
from opencomputer.agent.tool_result_storage import (
    PERSISTED_OUTPUT_TAG,
    enforce_turn_budget,
    generate_preview,
    maybe_persist_tool_result,
)


def test_generate_preview_short_content_returns_unchanged():
    preview, has_more = generate_preview("hello", max_chars=100)
    assert preview == "hello"
    assert has_more is False


def test_generate_preview_truncates_long():
    long = "a" * 500 + "\n" + "b" * 500
    preview, has_more = generate_preview(long, max_chars=200)
    assert len(preview) <= 200
    assert has_more is True


def test_maybe_persist_under_threshold_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out = maybe_persist_tool_result(
        content="short",
        tool_name="Bash",
        tool_use_id="t1",
    )
    assert out == "short"


def test_maybe_persist_over_threshold_spills_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    huge = "x" * 100_000
    cfg = BudgetConfig(turn_budget=200_000, preview_size=400)
    out = maybe_persist_tool_result(
        content=huge,
        tool_name="Bash",
        tool_use_id="t1",
        config=cfg,
        threshold=10_000,
    )
    assert PERSISTED_OUTPUT_TAG in out
    assert "t1.txt" in out
    # Verify the file was actually written
    spill_dir = tmp_path / "tool_result_storage"
    assert any(p.name.endswith("t1.txt") for p in spill_dir.rglob("*"))


def test_enforce_turn_budget_no_op_when_under(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    msgs = [{"content": "small", "tool_call_id": "t1"}]
    out = enforce_turn_budget(msgs)
    assert out[0]["content"] == "small"


def test_enforce_turn_budget_spills_largest(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    msgs = [
        {"content": "a" * 50_000, "tool_call_id": "small"},
        {"content": "b" * 200_000, "tool_call_id": "huge"},
        {"content": "c" * 50_000, "tool_call_id": "med"},
    ]
    cfg = BudgetConfig(turn_budget=100_000, preview_size=400)
    enforce_turn_budget(msgs, config=cfg)
    # Largest ('huge') should have been spilled
    assert PERSISTED_OUTPUT_TAG in msgs[1]["content"]
