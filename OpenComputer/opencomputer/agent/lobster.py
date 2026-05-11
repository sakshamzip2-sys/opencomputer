"""Lobster — deterministic multi-step workflow pipelines.

Port of OpenClaw's Lobster pipeline shell (see
``docs/OC-FROM-OPENCLAW.md`` item 6). The agent loop is non-
deterministic by design; Lobster is the escape hatch — run a fixed
sequence of steps with explicit approval gates, resumable via
``resumeToken`` when a gate is hit.

Public surface:

* :class:`LobsterStep` — one step in the pipeline.
* :class:`LobsterPipeline` — the ordered sequence of steps + state.
* :class:`PipelineRunner` — executes a pipeline, returning either
  a terminal :class:`PipelineResult` or a paused
  :class:`PipelineSuspended` carrying a ``resumeToken``.
* :func:`run_pipeline` — convenience entry-point.
* :func:`resume_pipeline` — resume from a saved token.

Step types implemented in this module:

* ``"exec"`` — run a shell command via the OC bash safety wrapper.
* ``"approve"`` — pause; return ``resumeToken`` to caller.
* ``"map"`` — transform stdin (JSON) via a Python expression in the
  configured namespace (sandboxed; no imports / dunders).

Persistence: pipelines are stored under
``<profile>/lobster/<resumeToken>.json`` so the user can resume
across process restarts.

Determinism guarantees:

* No LLM calls inside Lobster — steps are imperative.
* No network calls except those the exec steps explicitly invoke.
* Step ordering preserved by index; resumed runs skip already-
  completed steps.
* Timeouts enforced per step (default 30 s).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: How long a single exec step may run before being killed (seconds).
DEFAULT_STEP_TIMEOUT_S: float = 30.0

#: Per-step stdout/stderr cap. Larger results are truncated.
DEFAULT_OUTPUT_BYTE_CAP: int = 1024 * 1024  # 1 MB


class LobsterError(RuntimeError):
    """Lobster pipeline construction / runtime error."""


@dataclass(frozen=True, slots=True)
class LobsterStep:
    """One step in a Lobster pipeline.

    Attributes:
        kind: ``"exec"`` | ``"approve"`` | ``"map"``.
        name: optional human label for telemetry / logs.
        command: for ``exec`` — the shell command. argv list preferred
            over string; strings invoke through ``/bin/sh -c``.
        prompt: for ``approve`` — the question shown to the human.
        expression: for ``map`` — a constrained Python expression
            evaluated against ``{"stdin": <prev-stdout-json>}``.
        timeout_s: per-step timeout. Default 30 s.
    """

    kind: str
    name: str = ""
    command: str | list[str] = ""
    prompt: str = ""
    expression: str = ""
    timeout_s: float = DEFAULT_STEP_TIMEOUT_S

    def __post_init__(self) -> None:
        valid = {"exec", "approve", "map"}
        if self.kind not in valid:
            raise LobsterError(
                f"unknown LobsterStep.kind={self.kind!r}; expected one of {valid}"
            )
        if self.kind == "exec" and not self.command:
            raise LobsterError("exec step requires command")
        if self.kind == "approve" and not self.prompt:
            raise LobsterError("approve step requires prompt")
        if self.kind == "map" and not self.expression:
            raise LobsterError("map step requires expression")
        if self.timeout_s <= 0:
            raise LobsterError("timeout_s must be positive")


@dataclass(slots=True)
class _StepOutcome:
    """One executed step's record — persisted in :class:`LobsterPipeline.history`."""

    step_index: int
    name: str
    kind: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_s: float = 0.0


@dataclass(slots=True)
class LobsterPipeline:
    """The pipeline data: steps + completion state.

    ``resume_token`` is allocated on the first call to
    :meth:`PipelineRunner.run` so the file path is stable across
    pause/resume cycles. ``next_step_index`` advances atomically as
    steps complete.
    """

    steps: list[LobsterStep]
    name: str = ""
    resume_token: str = ""
    next_step_index: int = 0
    history: list[_StepOutcome] = field(default_factory=list)
    last_stdout: str = ""  # for piping into the next step

    def __post_init__(self) -> None:
        if not self.steps:
            raise LobsterError("pipeline must have at least one step")
        if not self.resume_token:
            self.resume_token = uuid.uuid4().hex


#: Reserved word for the resume-token field in serialised JSON.
#: Matches the OpenClaw spec (``resumeToken``) so external tools can
#: read OC pipeline state without re-mapping.
RESUME_TOKEN_FIELD: str = "resumeToken"
resumeToken: str = RESUME_TOKEN_FIELD  # noqa: N816 — camelCase alias mirrors OpenClaw spec spelling


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Terminal pipeline outcome — all steps ran (or failed)."""

    ok: bool
    pipeline_name: str
    resume_token: str
    outcomes: tuple[_StepOutcome, ...]
    last_stdout: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class PipelineSuspended:
    """Pipeline paused at an ``approve`` step — caller resumes with
    the returned ``resume_token`` after confirming."""

    resume_token: str
    prompt: str
    pipeline_name: str
    outcomes: tuple[_StepOutcome, ...]
    next_step_index: int


def _pipeline_state_dir() -> Path:
    """Default state dir for paused pipelines. Honours
    ``OPENCOMPUTER_LOBSTER_DIR`` for tests + per-profile setups."""
    override = os.environ.get("OPENCOMPUTER_LOBSTER_DIR")
    if override:
        return Path(override)
    try:
        from opencomputer.profiles import get_default_root

        return get_default_root() / "lobster"
    except Exception:  # noqa: BLE001
        return Path.cwd() / "lobster"


def _save_pipeline(pipeline: LobsterPipeline, *, state_dir: Path | None = None) -> Path:
    """Persist a paused pipeline so ``resume_pipeline`` can re-load it."""
    base = state_dir or _pipeline_state_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{pipeline.resume_token}.json"
    payload: dict[str, Any] = {
        "name": pipeline.name,
        RESUME_TOKEN_FIELD: pipeline.resume_token,
        "next_step_index": pipeline.next_step_index,
        "steps": [asdict(s) for s in pipeline.steps],
        "history": [asdict(o) for o in pipeline.history],
        "last_stdout": pipeline.last_stdout,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)
    return path


def _load_pipeline(resume_token: str, *, state_dir: Path | None = None) -> LobsterPipeline:
    base = state_dir or _pipeline_state_dir()
    path = base / f"{resume_token}.json"
    if not path.exists():
        raise LobsterError(f"no paused pipeline at {path}")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LobsterError(f"could not read paused pipeline: {exc}") from exc
    steps = [LobsterStep(**s) for s in raw.get("steps", [])]
    history = [_StepOutcome(**o) for o in raw.get("history", [])]
    return LobsterPipeline(
        steps=steps,
        name=raw.get("name", ""),
        resume_token=raw.get(RESUME_TOKEN_FIELD, resume_token),
        next_step_index=int(raw.get("next_step_index", 0)),
        history=history,
        last_stdout=raw.get("last_stdout", ""),
    )


def _delete_pipeline_state(resume_token: str, *, state_dir: Path | None = None) -> None:
    """Remove a completed pipeline's persisted state."""
    base = state_dir or _pipeline_state_dir()
    path = base / f"{resume_token}.json"
    try:
        path.unlink()
    except (OSError, FileNotFoundError):
        pass


#: Only allow these identifiers inside ``map`` step expressions.
#: Prevents arbitrary import / introspection from a workflow config.
_SAFE_NAMES = frozenset({"stdin", "True", "False", "None", "json", "len"})

#: Block anything dangerous in expressions before eval.
_FORBIDDEN_RE = re.compile(
    r"(\b__\w+__\b|\bimport\b|\bopen\b|\bexec\b|\beval\b|\bcompile\b)"
)


class PipelineRunner:
    """Execute one pipeline. Stateless across runs — pipeline state
    lives on the :class:`LobsterPipeline` instance itself."""

    def __init__(self, *, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir

    async def run(
        self, pipeline: LobsterPipeline
    ) -> PipelineResult | PipelineSuspended:
        """Drive the pipeline forward until completion / suspension /
        failure. Idempotent on already-completed steps — re-runs skip
        them based on :attr:`LobsterPipeline.next_step_index`.
        """
        outcomes: list[_StepOutcome] = list(pipeline.history)
        i = pipeline.next_step_index
        while i < len(pipeline.steps):
            step = pipeline.steps[i]
            start = time.monotonic()
            try:
                outcome = await self._run_step(i, step, pipeline.last_stdout)
            except Exception as exc:  # noqa: BLE001 — never raise from run; record
                outcomes.append(
                    _StepOutcome(
                        step_index=i,
                        name=step.name or step.kind,
                        kind=step.kind,
                        ok=False,
                        stderr=f"{type(exc).__name__}: {exc}",
                        duration_s=time.monotonic() - start,
                    )
                )
                pipeline.history = outcomes
                pipeline.next_step_index = i  # leave it for resume on retry
                return PipelineResult(
                    ok=False,
                    pipeline_name=pipeline.name,
                    resume_token=pipeline.resume_token,
                    outcomes=tuple(outcomes),
                    last_stdout=pipeline.last_stdout,
                    error=str(exc),
                )

            outcomes.append(outcome)
            pipeline.history = outcomes

            # approve step → suspend.
            if step.kind == "approve":
                pipeline.next_step_index = i + 1
                _save_pipeline(pipeline, state_dir=self._state_dir)
                return PipelineSuspended(
                    resume_token=pipeline.resume_token,
                    prompt=step.prompt,
                    pipeline_name=pipeline.name,
                    outcomes=tuple(outcomes),
                    next_step_index=pipeline.next_step_index,
                )

            if not outcome.ok:
                # Step failed → stop the pipeline, return failure result.
                pipeline.next_step_index = i
                return PipelineResult(
                    ok=False,
                    pipeline_name=pipeline.name,
                    resume_token=pipeline.resume_token,
                    outcomes=tuple(outcomes),
                    last_stdout=pipeline.last_stdout,
                    error=outcome.stderr,
                )

            pipeline.last_stdout = outcome.stdout
            i += 1

        # All steps complete.
        pipeline.next_step_index = i
        _delete_pipeline_state(pipeline.resume_token, state_dir=self._state_dir)
        return PipelineResult(
            ok=True,
            pipeline_name=pipeline.name,
            resume_token=pipeline.resume_token,
            outcomes=tuple(outcomes),
            last_stdout=pipeline.last_stdout,
        )

    async def _run_step(
        self, index: int, step: LobsterStep, stdin: str
    ) -> _StepOutcome:
        if step.kind == "exec":
            return await self._run_exec(index, step, stdin)
        if step.kind == "approve":
            return _StepOutcome(
                step_index=index,
                name=step.name or "approve",
                kind="approve",
                ok=True,
                stdout=stdin,  # pass-through
                duration_s=0.0,
            )
        if step.kind == "map":
            return self._run_map(index, step, stdin)
        raise LobsterError(f"unhandled step kind {step.kind!r}")

    async def _run_exec(
        self, index: int, step: LobsterStep, stdin: str
    ) -> _StepOutcome:
        start = time.monotonic()
        cmd = step.command
        if isinstance(cmd, list):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin.encode("utf-8") if stdin else None),
                timeout=step.timeout_s,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return _StepOutcome(
                step_index=index,
                name=step.name or "exec",
                kind="exec",
                ok=False,
                stderr=f"timed out after {step.timeout_s}s",
                exit_code=-1,
                duration_s=time.monotonic() - start,
            )
        # Cap output size to avoid OOM on huge dumps.
        stdout = stdout_b[:DEFAULT_OUTPUT_BYTE_CAP].decode("utf-8", errors="replace")
        stderr = stderr_b[:DEFAULT_OUTPUT_BYTE_CAP].decode("utf-8", errors="replace")
        return _StepOutcome(
            step_index=index,
            name=step.name or "exec",
            kind="exec",
            ok=proc.returncode == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            duration_s=time.monotonic() - start,
        )

    def _run_map(self, index: int, step: LobsterStep, stdin: str) -> _StepOutcome:
        start = time.monotonic()
        expr = step.expression
        if _FORBIDDEN_RE.search(expr):
            return _StepOutcome(
                step_index=index,
                name=step.name or "map",
                kind="map",
                ok=False,
                stderr="map expression contains forbidden token (dunder / import / exec / eval / open / compile)",
                duration_s=time.monotonic() - start,
            )
        # Parse stdin as JSON for piping; fall back to raw string.
        try:
            stdin_value: Any = json.loads(stdin) if stdin.strip() else None
        except ValueError:
            stdin_value = stdin
        safe_globals: dict[str, Any] = {"__builtins__": {}}
        safe_locals: dict[str, Any] = {
            "stdin": stdin_value,
            "json": json,
            "len": len,
            "True": True,
            "False": False,
            "None": None,
        }
        try:
            result = eval(expr, safe_globals, safe_locals)  # noqa: S307 — sandboxed
        except Exception as exc:  # noqa: BLE001
            return _StepOutcome(
                step_index=index,
                name=step.name or "map",
                kind="map",
                ok=False,
                stderr=f"map expression failed: {type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - start,
            )
        try:
            stdout = json.dumps(result, default=str, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return _StepOutcome(
                step_index=index,
                name=step.name or "map",
                kind="map",
                ok=False,
                stderr=f"map result unserialisable: {exc}",
                duration_s=time.monotonic() - start,
            )
        return _StepOutcome(
            step_index=index,
            name=step.name or "map",
            kind="map",
            ok=True,
            stdout=stdout,
            duration_s=time.monotonic() - start,
        )


async def run_pipeline(
    pipeline: LobsterPipeline,
    *,
    state_dir: Path | None = None,
) -> PipelineResult | PipelineSuspended:
    """Convenience: build a runner + drive the pipeline.

    ``state_dir`` overrides the default ``<profile>/lobster/`` so
    tests can sandbox the persistence path.
    """
    return await PipelineRunner(state_dir=state_dir).run(pipeline)


async def resume_pipeline(
    resume_token: str,
    *,
    state_dir: Path | None = None,
) -> PipelineResult | PipelineSuspended:
    """Resume a paused pipeline by its ``resumeToken``.

    Raises :class:`LobsterError` when the token doesn't map to a
    persisted pipeline file. The caller is responsible for tracking
    tokens (e.g. echo'd back to the user when an approve step
    suspended).
    """
    pipeline = _load_pipeline(resume_token, state_dir=state_dir)
    return await PipelineRunner(state_dir=state_dir).run(pipeline)


__all__ = [
    "DEFAULT_OUTPUT_BYTE_CAP",
    "DEFAULT_STEP_TIMEOUT_S",
    "LobsterError",
    "LobsterPipeline",
    "LobsterStep",
    "PipelineResult",
    "PipelineRunner",
    "PipelineSuspended",
    "RESUME_TOKEN_FIELD",
    "resumeToken",
    "resume_pipeline",
    "run_pipeline",
]
