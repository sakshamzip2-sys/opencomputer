"""``oc config set/get`` support for free-form dict sections (M3).

``Config.display`` is a plain dict, not a nested dataclass. Before M3,
``set_value`` rejected it ("'display' is not a config section"),
``get_value`` could not descend into it, and ``save_config`` dropped it
on write — so ``oc config set display.runtime_footer.enabled true``
(the command the docs tell users to run) silently no-opped.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config_store import (
    get_value,
    load_config,
    save_config,
    set_value,
)


def test_set_value_creates_nested_dict_path() -> None:
    cfg = load_config(Path("/nonexistent/config.yaml"))  # all-defaults
    assert cfg.display == {}
    new = set_value(cfg, "display.runtime_footer.enabled", True)
    assert new.display == {"runtime_footer": {"enabled": True}}
    # original Config is untouched (set_value returns a new one)
    assert cfg.display == {}


def test_get_value_descends_into_dict_section() -> None:
    cfg = set_value(
        load_config(Path("/nonexistent/c.yaml")),
        "display.runtime_footer.enabled",
        True,
    )
    assert get_value(cfg, "display.runtime_footer.enabled") is True


def test_get_value_unknown_dict_key_raises() -> None:
    cfg = load_config(Path("/nonexistent/c.yaml"))
    with pytest.raises(KeyError):
        get_value(cfg, "display.runtime_footer.enabled")  # display is {}


def test_set_value_preserves_sibling_dict_keys() -> None:
    cfg = load_config(Path("/nonexistent/c.yaml"))
    cfg = set_value(cfg, "display.skin", "charizard")
    cfg = set_value(cfg, "display.runtime_footer.enabled", True)
    assert cfg.display["skin"] == "charizard"
    assert cfg.display["runtime_footer"]["enabled"] is True


def test_display_round_trips_through_save_config(tmp_path: Path) -> None:
    """save_config must emit the display section, else oc config set no-ops."""
    cfg = load_config(Path("/nonexistent/c.yaml"))
    cfg = set_value(cfg, "display.runtime_footer.enabled", True)
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    reloaded = load_config(path)
    assert reloaded.display == {"runtime_footer": {"enabled": True}}


def test_empty_display_not_emitted(tmp_path: Path) -> None:
    """A config with no display section stays tidy — no empty block."""
    cfg = load_config(Path("/nonexistent/c.yaml"))
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    assert "display:" not in path.read_text(encoding="utf-8")
