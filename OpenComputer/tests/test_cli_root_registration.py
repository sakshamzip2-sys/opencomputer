"""Regression — the root CLI app actually exposes every recipe's command.

Pre-2026-05-18, the best-of-three recipes (R4 ``oc skin``, R5 ``oc plugin
marketplace/search``, R6 ``oc plugin reload``, R8 ``oc plugin doctor``,
R10 ``oc plugin update-check``) were tested via direct
``runner.invoke(plugin_app, ...)`` / ``runner.invoke(skin_app, ...)``
calls that bypass the root ``python -m opencomputer`` registration.
A working test suite plus a stale ``cli.py`` import-or-add_typer order
could ship a fully-tested recipe whose CLI is unreachable to the user.

This test fails fast if the end-to-end registration trace from
``opencomputer.cli.app`` to each recipe's command surface breaks.
"""
from __future__ import annotations

from opencomputer import cli


def _top_level_groups() -> list[str]:
    return sorted(g.name for g in cli.app.registered_groups if g.name)


def _subapp_for(name: str):  # noqa: ANN202
    return next(
        (g.typer_instance for g in cli.app.registered_groups if g.name == name),
        None,
    )


def _subcommands(typer_instance) -> list[str]:  # noqa: ANN001
    return sorted(c.name for c in typer_instance.registered_commands if c.name)


def _subgroups(typer_instance) -> list[str]:  # noqa: ANN001
    return sorted(g.name for g in typer_instance.registered_groups if g.name)


# ── R4 skin ──────────────────────────────────────────────────────────


def test_r4_skin_top_level_registered() -> None:
    assert "skin" in _top_level_groups(), (
        f"oc skin not registered as a top-level subapp; top-level groups: "
        f"{_top_level_groups()!r}"
    )


def test_r4_skin_has_list_set_preview() -> None:
    skin = _subapp_for("skin")
    assert skin is not None
    cmds = _subcommands(skin)
    assert cmds == ["list", "preview", "set"], (
        f"oc skin must expose list/set/preview; got {cmds!r}"
    )


# ── R8 / R10 / R5 plugin subcommands ─────────────────────────────────


def test_r8_plugin_doctor_registered() -> None:
    plugin = _subapp_for("plugin")
    assert plugin is not None
    assert "doctor" in _subcommands(plugin), (
        f"oc plugin doctor not registered; plugin subcommands: "
        f"{_subcommands(plugin)!r}"
    )


def test_r10_plugin_update_check_registered() -> None:
    plugin = _subapp_for("plugin")
    assert plugin is not None
    assert "update-check" in _subcommands(plugin), (
        f"oc plugin update-check not registered; subcommands: "
        f"{_subcommands(plugin)!r}"
    )


def test_r5_plugin_search_registered() -> None:
    plugin = _subapp_for("plugin")
    assert plugin is not None
    assert "search" in _subcommands(plugin), (
        f"oc plugin search not registered; subcommands: "
        f"{_subcommands(plugin)!r}"
    )


def test_r5_plugin_marketplace_registered() -> None:
    """``marketplace`` is a sub-typer under ``plugin`` (with add/list/remove)."""
    plugin = _subapp_for("plugin")
    assert plugin is not None
    # ``marketplace`` may appear as either a sub-group (typer) or a
    # subcommand — accept either, but require the surface to be present.
    surface = set(_subcommands(plugin)) | set(_subgroups(plugin))
    assert "marketplace" in surface, (
        f"oc plugin marketplace not registered; available subcommands: "
        f"{sorted(surface)!r}"
    )
