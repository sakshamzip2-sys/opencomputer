"""A6 — per-turn working-directory ContextVar."""
from __future__ import annotations

import asyncio
import os

from plugin_sdk.working_directory import (
    get_working_directory,
    working_directory,
)


def test_unbound_falls_back_to_process_cwd() -> None:
    assert get_working_directory() == os.getcwd()


def test_bound_value_is_returned() -> None:
    with working_directory("/tmp/some/project"):
        assert get_working_directory() == "/tmp/some/project"
    # Reset on exit.
    assert get_working_directory() == os.getcwd()


def test_falsy_path_is_a_noop() -> None:
    with working_directory(None):
        assert get_working_directory() == os.getcwd()
    with working_directory(""):
        assert get_working_directory() == os.getcwd()


def test_reset_happens_on_exception() -> None:
    try:
        with working_directory("/tmp/x"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert get_working_directory() == os.getcwd()


def test_nested_bindings_restore_outer() -> None:
    with working_directory("/tmp/outer"):
        assert get_working_directory() == "/tmp/outer"
        with working_directory("/tmp/inner"):
            assert get_working_directory() == "/tmp/inner"
        assert get_working_directory() == "/tmp/outer"


def test_contextvar_isolates_concurrent_tasks() -> None:
    """Two concurrent tasks must not see each other's bound cwd —
    the gateway runs many sessions in one process."""

    async def _task(path: str, results: dict[str, str]) -> None:
        with working_directory(path):
            await asyncio.sleep(0)  # yield to the other task
            results[path] = get_working_directory()

    async def _main() -> dict[str, str]:
        results: dict[str, str] = {}
        await asyncio.gather(
            _task("/tmp/a", results),
            _task("/tmp/b", results),
        )
        return results

    results = asyncio.run(_main())
    assert results == {"/tmp/a": "/tmp/a", "/tmp/b": "/tmp/b"}
