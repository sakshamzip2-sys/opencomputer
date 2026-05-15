from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext


def test_seed_chat_status_metadata_populates_model_before_first_prompt() -> None:
    from opencomputer.cli import _seed_chat_status_metadata

    class _Model:
        model = "claude-opus-4-7"

    class _Cfg:
        model = _Model()
        model_context_overrides = {"claude-opus-4-7": 1_000_000}
        custom_providers = ()

    rt = RuntimeContext()
    _seed_chat_status_metadata(rt, _Cfg())

    assert rt.custom["model_id"] == "claude-opus-4-7"
    assert rt.custom["model_context_overrides"] == {"claude-opus-4-7": 1_000_000}
    assert rt.custom["custom_providers"] == ()


def test_chat_loop_prompt_reads_seeded_loop_runtime() -> None:
    import inspect

    from opencomputer import cli

    src = inspect.getsource(cli._run_chat_session)
    assert "_seed_chat_status_metadata(runtime, cfg)" in src
    assert "loop._runtime = runtime" in src
