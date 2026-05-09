"""Tests for /batch production wiring (M11.2 follow-up)."""

from __future__ import annotations

import pytest

from opencomputer.agent.batch_orchestrator import (
    BatchConfig,
    BatchUnit,
    UnitOutcome,
)
from opencomputer.agent.batch_runner import (
    DEFAULT_BATCH_ALLOWED_TOOLS,
    MissingPRUrlError,
    _extract_pr_url,
    _format_unit_prompt,
    make_delegate_spawn_fn,
    run_batch_via_delegate,
)
from plugin_sdk.core import ToolCall, ToolResult

# ─── _extract_pr_url ─────────────────────────────────────────────


def test_extract_pr_url_finds_first_match() -> None:
    body = (
        "Done.\n\nOpened https://github.com/foo/bar/pull/123 with the change.\n"
    )
    assert _extract_pr_url(body) == "https://github.com/foo/bar/pull/123"


def test_extract_pr_url_handles_trailing_punctuation() -> None:
    body = "PR: https://github.com/foo/bar/pull/42)."
    assert _extract_pr_url(body) == "https://github.com/foo/bar/pull/42"


def test_extract_pr_url_picks_first_when_multiple() -> None:
    body = (
        "Tests pass; opened https://github.com/foo/bar/pull/1 — "
        "see also https://github.com/foo/bar/pull/2"
    )
    assert _extract_pr_url(body) == "https://github.com/foo/bar/pull/1"


def test_extract_pr_url_empty_response_raises() -> None:
    with pytest.raises(MissingPRUrlError, match="empty"):
        _extract_pr_url("")


def test_extract_pr_url_no_url_raises() -> None:
    with pytest.raises(MissingPRUrlError, match="no GitHub PR URL"):
        _extract_pr_url("Done — but I forgot to open a PR.")


def test_extract_pr_url_rejects_issue_url() -> None:
    """Issue URLs (`/issues/N`) are not PRs; must not match."""
    body = "https://github.com/foo/bar/issues/99"
    with pytest.raises(MissingPRUrlError):
        _extract_pr_url(body)


# ─── _format_unit_prompt ─────────────────────────────────────────


def test_format_unit_prompt_includes_description() -> None:
    unit = BatchUnit(unit_id="u1", description="rename foo → bar in baz.py")
    out = _format_unit_prompt(unit, pr_title_prefix="batch")
    assert "rename foo → bar in baz.py" in out
    assert "batch: u1" in out
    assert "PR URL on the last line" in out


def test_format_unit_prompt_with_verification() -> None:
    unit = BatchUnit(
        unit_id="u2",
        description="run codemod",
        verify="pytest tests/test_baz.py -x",
    )
    out = _format_unit_prompt(unit, pr_title_prefix="codemod")
    assert "Verification:" in out
    assert "pytest tests/test_baz.py" in out


def test_format_unit_prompt_no_verification() -> None:
    unit = BatchUnit(unit_id="u3", description="patch", verify="")
    out = _format_unit_prompt(unit, pr_title_prefix="x")
    assert "Verification:" not in out


# ─── make_delegate_spawn_fn ─────────────────────────────────────


class _FakeDelegate:
    """Captures the ToolCall and returns a canned ToolResult."""

    def __init__(self, response_text: str = "", is_error: bool = False) -> None:
        self.response_text = response_text
        self.is_error = is_error
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            tool_call_id=call.id,
            content=self.response_text,
            is_error=self.is_error,
        )


@pytest.mark.asyncio
async def test_spawn_fn_returns_pr_url_on_success() -> None:
    fake = _FakeDelegate(
        response_text="Done. https://github.com/owner/repo/pull/7"
    )
    spawn_fn = make_delegate_spawn_fn(fake)
    unit = BatchUnit(unit_id="x", description="rename")
    url = await spawn_fn(unit)
    assert url == "https://github.com/owner/repo/pull/7"


@pytest.mark.asyncio
async def test_spawn_fn_passes_isolation_worktree() -> None:
    fake = _FakeDelegate(response_text="https://github.com/o/r/pull/1")
    spawn_fn = make_delegate_spawn_fn(fake)
    await spawn_fn(BatchUnit(unit_id="u", description="d"))
    assert fake.calls[0].arguments["isolation"] == "worktree"


@pytest.mark.asyncio
async def test_spawn_fn_uses_default_allowlist() -> None:
    fake = _FakeDelegate(response_text="https://github.com/o/r/pull/1")
    spawn_fn = make_delegate_spawn_fn(fake)
    await spawn_fn(BatchUnit(unit_id="u", description="d"))
    allowed = fake.calls[0].arguments["allowed_tools"]
    for tool in DEFAULT_BATCH_ALLOWED_TOOLS:
        assert tool in allowed
    # Delegate / WebFetch / WebSearch must NOT be in the allowlist —
    # batch leaves are leaves, not orchestrators.
    assert "Delegate" not in allowed
    assert "WebFetch" not in allowed


@pytest.mark.asyncio
async def test_spawn_fn_role_leaf_prevents_recursion() -> None:
    """``role='leaf'`` prevents a batch unit from spawning its own
    sub-batches (defence-in-depth on top of ``allowed_tools``)."""
    fake = _FakeDelegate(response_text="https://github.com/o/r/pull/1")
    spawn_fn = make_delegate_spawn_fn(fake)
    await spawn_fn(BatchUnit(unit_id="u", description="d"))
    assert fake.calls[0].arguments["role"] == "leaf"


@pytest.mark.asyncio
async def test_spawn_fn_passes_empty_paths_when_unit_has_none() -> None:
    """Today's BatchUnit has no path field; the spawn_fn passes [] so
    DelegateTool's path-coordinator no-ops on overlap detection."""
    fake = _FakeDelegate(response_text="https://github.com/o/r/pull/1")
    spawn_fn = make_delegate_spawn_fn(fake)
    await spawn_fn(BatchUnit(unit_id="u", description="d"))
    assert fake.calls[0].arguments["paths"] == []


@pytest.mark.asyncio
async def test_spawn_fn_raises_on_delegate_error() -> None:
    fake = _FakeDelegate(response_text="boom", is_error=True)
    spawn_fn = make_delegate_spawn_fn(fake)
    with pytest.raises(RuntimeError, match="DelegateTool returned error"):
        await spawn_fn(BatchUnit(unit_id="u", description="d"))


@pytest.mark.asyncio
async def test_spawn_fn_raises_when_no_pr_url() -> None:
    fake = _FakeDelegate(response_text="Done — but no PR opened.")
    spawn_fn = make_delegate_spawn_fn(fake)
    with pytest.raises(MissingPRUrlError):
        await spawn_fn(BatchUnit(unit_id="u", description="d"))


@pytest.mark.asyncio
async def test_spawn_fn_propagates_exception() -> None:
    """An exception inside DelegateTool.execute bubbles up so the
    orchestrator records the unit as FAILED."""

    class _RaisingFake:
        async def execute(self, _call: ToolCall) -> ToolResult:
            raise RuntimeError("delegate exploded")

    spawn_fn = make_delegate_spawn_fn(_RaisingFake())
    with pytest.raises(RuntimeError, match="delegate exploded"):
        await spawn_fn(BatchUnit(unit_id="u", description="d"))


# ─── run_batch_via_delegate (end-to-end glue) ────────────────────


@pytest.mark.asyncio
async def test_run_batch_via_delegate_aggregates_pr_urls() -> None:
    """Two units, both succeed → result has two PR URLs."""

    class _MultiFake:
        def __init__(self) -> None:
            self.counter = 0

        async def execute(self, call: ToolCall) -> ToolResult:
            self.counter += 1
            return ToolResult(
                tool_call_id=call.id,
                content=f"https://github.com/o/r/pull/{self.counter}",
                is_error=False,
            )

    units = [
        BatchUnit(unit_id="a", description="task A"),
        BatchUnit(unit_id="b", description="task B"),
    ]
    result = await run_batch_via_delegate(
        units, delegate_tool=_MultiFake(), config=BatchConfig(max_parallel=2)
    )
    pr_urls = {r.pr_url for r in result.units if r.pr_url}
    assert len(pr_urls) == 2
    assert all(u.startswith("https://github.com/o/r/pull/") for u in pr_urls)


@pytest.mark.asyncio
async def test_run_batch_via_delegate_one_unit_fails_other_succeeds() -> None:
    """Sibling failures don't abort the batch."""

    class _PartialFake:
        async def execute(self, call: ToolCall) -> ToolResult:
            if "fail-me" in call.arguments["task"]:
                return ToolResult(
                    tool_call_id=call.id, content="Done — no PR.", is_error=False,
                )
            return ToolResult(
                tool_call_id=call.id,
                content="https://github.com/o/r/pull/9",
                is_error=False,
            )

    units = [
        BatchUnit(unit_id="ok", description="normal task"),
        BatchUnit(unit_id="bad", description="fail-me intentionally"),
    ]
    result = await run_batch_via_delegate(
        units, delegate_tool=_PartialFake(), config=BatchConfig(max_parallel=2)
    )
    by_id = {r.unit_id: r for r in result.units}
    assert by_id["ok"].outcome == UnitOutcome.SUCCESS
    assert by_id["ok"].pr_url == "https://github.com/o/r/pull/9"
    assert by_id["bad"].outcome == UnitOutcome.FAILED
