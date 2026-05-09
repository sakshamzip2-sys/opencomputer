"""v1.1 plan-4 M13 — core verb collisions are always fatal.

A plugin trying to register `oc chat` (or any other core verb) must be
rejected at registration time, regardless of `replace=`.
"""

from __future__ import annotations

import pytest
import typer

from opencomputer.plugins.cli_registry import CORE_RESERVED_CLI_NAMES
from opencomputer.plugins.loader import PluginAPI, PluginCLINameCollision


def _bare_api() -> PluginAPI:
    return PluginAPI(
        tool_registry=None,  # type: ignore[arg-type]
        hook_engine=None,
        provider_registry={},
        channel_registry={},
    )


@pytest.mark.parametrize(
    "core_verb",
    sorted(
        {
            "chat",
            "gateway",
            "wire",
            "doctor",
            "setup",
            "plugin",
            "profile",
            "preset",
            "cron",
            "consent",
            "skills",
            "checkpoints",
            "rules",
        }
    ),
)
def test_core_verb_collision_raises(core_verb: str) -> None:
    api = _bare_api()
    sub = typer.Typer()
    with pytest.raises(PluginCLINameCollision) as exc_info:
        api.register_cli_command(core_verb, sub)
    assert "reserved core verb" in str(exc_info.value)


def test_replace_true_does_not_bypass_core_collision() -> None:
    """replace=True is for plugin-vs-plugin only. Core verbs stay fatal."""
    api = _bare_api()
    with pytest.raises(PluginCLINameCollision):
        api.register_cli_command("chat", typer.Typer(), replace=True)


def test_core_reserved_set_is_non_empty() -> None:
    """Sanity: the reserved set must include at least the spec's call-out list."""
    spec_must_reserve = {
        "chat",
        "gateway",
        "wire",
        "doctor",
        "setup",
        "plugin",
        "profile",
        "preset",
        "cron",
        "consent",
        "skills",
        "session",
        "checkpoints",
        "rules",
    }
    missing = spec_must_reserve - CORE_RESERVED_CLI_NAMES
    assert not missing, f"core-reserved set is missing spec verbs: {missing}"
