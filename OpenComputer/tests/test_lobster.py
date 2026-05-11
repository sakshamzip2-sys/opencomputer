"""Tests for opencomputer.agent.lobster — deterministic workflow pipelines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.lobster import (
    RESUME_TOKEN_FIELD,
    LobsterError,
    LobsterPipeline,
    LobsterStep,
    PipelineResult,
    PipelineSuspended,
    resume_pipeline,
    resumeToken,
    run_pipeline,
)

# ─── Step validation ───────────────────────────────────────────────────


class TestStepValidation:
    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(LobsterError, match="unknown"):
            LobsterStep(kind="bogus")

    def test_exec_requires_command(self) -> None:
        with pytest.raises(LobsterError, match="command"):
            LobsterStep(kind="exec")

    def test_approve_requires_prompt(self) -> None:
        with pytest.raises(LobsterError, match="prompt"):
            LobsterStep(kind="approve")

    def test_map_requires_expression(self) -> None:
        with pytest.raises(LobsterError, match="expression"):
            LobsterStep(kind="map")

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(LobsterError, match="timeout"):
            LobsterStep(kind="exec", command="echo hi", timeout_s=0)


class TestPipelineValidation:
    def test_empty_steps_rejected(self) -> None:
        with pytest.raises(LobsterError):
            LobsterPipeline(steps=[])

    def test_resume_token_generated(self) -> None:
        p = LobsterPipeline(steps=[LobsterStep(kind="exec", command="echo hi")])
        assert len(p.resume_token) == 32  # uuid4 hex


# ─── Exec steps ────────────────────────────────────────────────────────


class TestExecSteps:
    @pytest.mark.asyncio
    async def test_simple_echo(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            name="echo",
            steps=[LobsterStep(kind="exec", command=["echo", "hello"])],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is True
        assert result.last_stdout.strip() == "hello"

    @pytest.mark.asyncio
    async def test_failed_step_stops_pipeline(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command="false"),
                LobsterStep(kind="exec", command="echo should-not-run"),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is False
        # Only the failing step ran.
        assert len(result.outcomes) == 1
        assert result.outcomes[0].ok is False

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command="sleep 5", timeout_s=0.1),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is False
        assert "timed out" in result.outcomes[0].stderr

    @pytest.mark.asyncio
    async def test_stdin_piped_to_next(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", "data"]),
                LobsterStep(kind="exec", command=["cat"]),  # echoes stdin
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is True
        assert "data" in result.last_stdout


# ─── Map steps ─────────────────────────────────────────────────────────


class TestMapSteps:
    @pytest.mark.asyncio
    async def test_map_transforms_json(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", '{"x": 10}']),
                LobsterStep(kind="map", expression="stdin['x'] * 2"),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is True
        assert json.loads(result.last_stdout) == 20

    @pytest.mark.asyncio
    async def test_map_rejects_imports(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", "x"]),
                LobsterStep(kind="map", expression="__import__('os').getcwd()"),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is False
        assert "forbidden" in result.outcomes[-1].stderr

    @pytest.mark.asyncio
    async def test_map_handles_eval_error(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", "x"]),
                LobsterStep(kind="map", expression="1/0"),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineResult)
        assert result.ok is False
        assert "ZeroDivision" in result.outcomes[-1].stderr


# ─── Approve / suspend / resume ────────────────────────────────────────


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_approve_suspends_with_token(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", "before"]),
                LobsterStep(kind="approve", prompt="apply changes?"),
                LobsterStep(kind="exec", command=["echo", "after"]),
            ],
        )
        result = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(result, PipelineSuspended)
        assert result.prompt == "apply changes?"
        assert len(result.resume_token) > 0
        # State file persisted.
        assert (tmp_path / f"{result.resume_token}.json").exists()

    @pytest.mark.asyncio
    async def test_resume_continues(self, tmp_path: Path) -> None:
        p = LobsterPipeline(
            steps=[
                LobsterStep(kind="exec", command=["echo", "before"]),
                LobsterStep(kind="approve", prompt="ok?"),
                LobsterStep(kind="exec", command=["echo", "after"]),
            ],
        )
        suspended = await run_pipeline(p, state_dir=tmp_path)
        assert isinstance(suspended, PipelineSuspended)
        resumed = await resume_pipeline(suspended.resume_token, state_dir=tmp_path)
        assert isinstance(resumed, PipelineResult)
        assert resumed.ok is True
        assert resumed.last_stdout.strip() == "after"
        # State file cleaned up after completion.
        assert not (tmp_path / f"{suspended.resume_token}.json").exists()

    @pytest.mark.asyncio
    async def test_resume_with_bogus_token(self, tmp_path: Path) -> None:
        with pytest.raises(LobsterError):
            await resume_pipeline("not-a-real-token", state_dir=tmp_path)


# ─── Parity-doctor spec names ──────────────────────────────────────────


class TestParityNames:
    def test_resume_token_alias_exists(self) -> None:
        # Both spellings must be importable from the module surface.
        assert resumeToken == RESUME_TOKEN_FIELD
        assert RESUME_TOKEN_FIELD == "resumeToken"
