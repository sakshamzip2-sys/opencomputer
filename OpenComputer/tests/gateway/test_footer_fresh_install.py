"""M1 / T1.8 — runtime_footer enabled-by-default for fresh installs.

A fresh install via ``oc config init --variant <name>`` copies a bundled
variant YAML that now carries ``display.runtime_footer.enabled: true``.
Existing ``config.yaml`` files (and ``default_config()``) have no
``display:`` section, so they keep the historical OFF default — no
surprise behaviour change on upgrade.

Also covers the wiring fix this task uncovered: before M1 the top-level
``display:`` section was dropped by ``load_config`` (no ``Config.display``
field) and never reached the gateway dispatcher, so the footer knob was
dead. ``Config`` now carries it and ``Gateway`` forwards it to
``Dispatch``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import default_config
from opencomputer.agent.config_store import load_config
from opencomputer.gateway.runtime_footer import resolve_footer_config

_VARIANTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "opencomputer"
    / "settings_variants"
)


@pytest.mark.parametrize("variant", ["lax", "strict", "sandbox"])
def test_fresh_install_variant_enables_footer(variant: str) -> None:
    cfg = load_config(_VARIANTS_DIR / f"{variant}.yaml")
    # The display section survives load_config into Config.display...
    assert cfg.display.get("runtime_footer", {}).get("enabled") is True
    # ...and resolves to an enabled footer.
    fc = resolve_footer_config({"display": cfg.display})
    assert fc.enabled is True


def test_default_config_keeps_footer_off() -> None:
    """No display section → existing installs see no behaviour change."""
    cfg = default_config()
    assert cfg.display == {}
    assert resolve_footer_config({"display": cfg.display}).enabled is False


def test_config_without_display_section_is_off(tmp_path: Path) -> None:
    """A hand-written config.yaml with no display: keeps the footer off."""
    p = tmp_path / "config.yaml"
    p.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.display == {}
    assert resolve_footer_config({"display": cfg.display}).enabled is False


def test_variants_still_parse_after_display_added() -> None:
    """Each variant must round-trip — config_init re-parses as a smoke test."""
    for variant in ("lax", "strict", "sandbox"):
        cfg = load_config(_VARIANTS_DIR / f"{variant}.yaml")
        assert cfg.model.provider  # non-empty — the file parsed fully
