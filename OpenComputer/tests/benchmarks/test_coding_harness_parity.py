"""Coding-harness parity benchmark — measures OC's agent loop on canonical tasks.

V3.A-T0: this is the BASELINE INFRASTRUCTURE for tracking how prompt-, tool-
description-, and error-message-engineering changes (V3.A-T3 through T6) move
the needle on a 5-task canonical workload. It is NOT a CI gate.

Each task is a self-contained scenario. The benchmark runs the task end-to-end
through ``opencomputer.agent.loop.AgentLoop.run_conversation`` and records:

    - ``tool_calls``     : count of tool invocations (sum of ``len(msg.tool_calls)``
                           across all assistant messages in the result)
    - ``iterations``     : agent loop iterations (taken from ``ConversationResult.iterations``)
    - ``elapsed_seconds``: wall-clock time around ``run_conversation``
    - ``success``        : did the task's verification predicate pass?

This is a CHECKPOINT benchmark, run locally by whoever has API keys. The test
SKIPS gracefully when no provider is configured (no ``ANTHROPIC_API_KEY`` or
no anthropic-provider plugin loaded). To opt in:

    pytest -m benchmark tests/benchmarks/test_coding_harness_parity.py -v

Running this without ``-m benchmark`` collects but skips all five cases — that
is the expected behaviour on CI. The infrastructure (parametrization, fixture
setup, verification predicates, metric extraction) is what V3.A-T0 ships.

After T3-T6 each land, the maintainer with API keys re-runs locally and writes
the comparison table into the corresponding PR body.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """One benchmark task's recorded metrics."""

    task_id: str
    tool_calls: int
    iterations: int
    elapsed_seconds: float
    success: bool


# Five canonical tasks that exercise the harness's core capabilities.
BENCHMARK_TASKS: tuple[tuple[str, str], ...] = (
    (
        "refactor_function",
        "Refactor the `add` function in {tmp}/sample.py to take a list of ints instead of two args. Update tests.",
    ),
    (
        "add_test",
        "Add a pytest test for the `multiply(a, b)` function in {tmp}/calc.py covering negative inputs.",
    ),
    (
        "fix_type_error",
        "Fix the mypy errors in {tmp}/typed.py without changing behavior.",
    ),
    (
        "write_script",
        "Write a Python script at {tmp}/count_lines.py that counts non-empty lines in any text file passed as argv[1].",
    ),
    (
        "debug_failure",
        "The test in {tmp}/buggy_test.py is failing. Read it, find the bug in {tmp}/buggy.py, fix it.",
    ),
)


@pytest.mark.benchmark
@pytest.mark.parametrize("task_id,prompt_template", BENCHMARK_TASKS)
def test_benchmark_task(task_id: str, prompt_template: str, tmp_path: Path) -> None:
    """Run one benchmark task end-to-end through OC's agent loop.

    SKIPPED unless invoked with ``pytest -m benchmark``. ALSO skipped when the
    Anthropic provider plugin or its API key is not available — that's the
    "no API keys on CI" guard.

    Records the four metrics to ``tmp_path/.benchmark_<task_id>.json`` so a
    subsequent comparison run can diff baseline vs candidate.
    """
    # Lazy import — keeps collection-time side-effects minimal.
    from opencomputer.agent.config import Config
    from opencomputer.agent.loop import AgentLoop

    provider = _resolve_provider_or_skip()

    _setup_fixture(task_id, tmp_path)
    prompt = prompt_template.format(tmp=str(tmp_path))

    config = Config()
    loop = AgentLoop(provider=provider, config=config)

    started = time.monotonic()
    result = _run_to_completion(loop, prompt)
    elapsed = time.monotonic() - started

    success = _verify_task(task_id, tmp_path)

    bench = BenchmarkResult(
        task_id=task_id,
        tool_calls=result["tool_calls"],
        iterations=result["iterations"],
        elapsed_seconds=elapsed,
        success=success,
    )

    out_path = tmp_path / f".benchmark_{task_id}.json"
    out_path.write_text(_to_json(bench))

    # Soft assertion: in V3.A-T0 the bar is "infrastructure compiles and runs."
    # Whether each task succeeds is the metric we're measuring AFTER T3-T6,
    # not a property the scaffold itself enforces. Failures here are signal,
    # not regressions — they're the gap we're trying to close.
    assert success, (
        f"Task {task_id} did not complete successfully. "
        f"Metrics: tool_calls={bench.tool_calls}, iterations={bench.iterations}, "
        f"elapsed={bench.elapsed_seconds:.1f}s. This is expected behaviour at "
        f"V3.A-T0 baseline; T3-T6 prompt + description + error-message rewrites "
        f"are the work that closes this gap."
    )


def _resolve_provider_or_skip() -> Any:
    """Return an instantiated provider, or ``pytest.skip`` if unavailable.

    The benchmark deliberately does NOT mock the provider — the whole point is
    to measure the real loop's behaviour with a real model. CI runs without
    API keys SKIP cleanly; local runs with ``ANTHROPIC_API_KEY`` set run the
    full benchmark.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "Coding-harness parity benchmark requires a live LLM provider. "
            "Set ANTHROPIC_API_KEY (or another provider's env) and run with "
            "'pytest -m benchmark' to record metrics."
        )

    # Mirror cli._resolve_provider — query the plugin registry, bail clearly
    # if no provider plugin is loaded.
    try:
        from opencomputer.plugins.discovery import discover_plugins
        from opencomputer.plugins.registry import registry as plugin_registry

        # Best-effort plugin discovery — load extensions/ if not already loaded.
        try:
            discover_plugins()
        except Exception:  # noqa: BLE001 — discovery may have already run
            pass

        registered = plugin_registry.providers.get("anthropic")
        if registered is None:
            pytest.skip(
                "No 'anthropic' provider plugin registered. Install or enable "
                "extensions/anthropic-provider/ to run the benchmark."
            )
        return registered() if isinstance(registered, type) else registered
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Provider resolution failed: {exc}")


def _setup_fixture(task_id: str, tmp_path: Path) -> None:
    """Seed tmp_path with the input files each task needs."""
    if task_id == "refactor_function":
        (tmp_path / "sample.py").write_text("def add(a, b):\n    return a + b\n")
        (tmp_path / "test_sample.py").write_text(
            "from sample import add\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
    elif task_id == "add_test":
        (tmp_path / "calc.py").write_text("def multiply(a, b):\n    return a * b\n")
    elif task_id == "fix_type_error":
        (tmp_path / "typed.py").write_text(
            "def greet(name: str) -> str:\n    return name + 1  # type error\n"
        )
    elif task_id == "write_script":
        # Blank — the script itself is the deliverable.
        pass
    elif task_id == "debug_failure":
        (tmp_path / "buggy.py").write_text("def divide(a, b):\n    return a + b  # bug\n")
        (tmp_path / "buggy_test.py").write_text(
            "from buggy import divide\ndef test_divide():\n    assert divide(10, 2) == 5\n"
        )


def _verify_task(task_id: str, tmp_path: Path) -> bool:
    """Return True if the task's success criterion is met.

    Predicates are deliberately GENEROUS — they assert on the END state, not
    the path. Many valid solutions exist for each task; the verifier should
    not punish creativity. Tighten only when false positives appear during
    real baseline runs.
    """
    if task_id == "refactor_function":
        sample_path = tmp_path / "sample.py"
        if not sample_path.exists():
            return False
        sample = sample_path.read_text()
        return "def add(" in sample and ("list" in sample or "[" in sample)
    if task_id == "add_test":
        try:
            test_files = list(tmp_path.glob("test_*.py")) + list(tmp_path.glob("*_test.py"))
            return any(
                "multiply" in p.read_text() and "negative" in p.read_text().lower()
                for p in test_files
            )
        except OSError:
            return False
    if task_id == "fix_type_error":
        typed_path = tmp_path / "typed.py"
        if not typed_path.exists():
            return False
        typed = typed_path.read_text()
        return "+ 1" not in typed
    if task_id == "write_script":
        return (tmp_path / "count_lines.py").exists()
    if task_id == "debug_failure":
        buggy_path = tmp_path / "buggy.py"
        if not buggy_path.exists():
            return False
        buggy = buggy_path.read_text()
        return "/" in buggy or "//" in buggy
    return False


def _run_to_completion(loop: Any, prompt: str) -> dict[str, int]:
    """Drive the loop to completion and return tool-call + iteration counts.

    Iterations come straight from ``ConversationResult.iterations`` — the loop
    increments this on every LLM round-trip, capped at
    ``config.loop.max_iterations``. Tool calls are counted by walking
    ``result.messages`` for assistant messages with non-empty ``tool_calls``;
    each ``ToolCall`` in the list is one invocation.

    The loop terminates on its own when the model returns ``stop_reason ==
    end_turn`` (no more tool_use blocks) — we don't need an external timeout
    because ``Config.loop.iteration_timeout_s`` and ``inactivity_timeout_s``
    enforce wall-clock bounds.
    """
    import asyncio

    result = asyncio.run(loop.run_conversation(user_message=prompt))

    tool_calls = 0
    for msg in result.messages:
        if msg.role == "assistant" and msg.tool_calls:
            tool_calls += len(msg.tool_calls)

    return {
        "tool_calls": tool_calls,
        "iterations": result.iterations,
    }


def _to_json(b: BenchmarkResult) -> str:
    """Serialise a BenchmarkResult to JSON. Stable key order for diffability."""
    return json.dumps(
        {
            "task_id": b.task_id,
            "tool_calls": b.tool_calls,
            "iterations": b.iterations,
            "elapsed_seconds": b.elapsed_seconds,
            "success": b.success,
        },
        indent=2,
        sort_keys=True,
    )
