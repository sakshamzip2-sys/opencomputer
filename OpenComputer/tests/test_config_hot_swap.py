"""Tests for §9.4 config hot-swap — see
``docs/plans/profile-handoff-investigation.md``.

Coverage:
  - Allowlisted fields are applied; non-allowlisted fields are reported
    in skipped_restart_required
  - No-op when nothing changed
  - load_config_for_profile error doesn't raise, returns error string
  - dataclasses.replace fails → original config preserved
  - Field-delta detector correctness
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _write_yaml(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_swap_to_same_profile_is_no_op(tmp_path: Path) -> None:
    """Swapping to the SAME profile root → no fields apply."""
    from opencomputer.agent.config_hot_swap import _load_profile_config, hot_swap_config

    old = _load_profile_config(tmp_path)
    merged, result = hot_swap_config(old, tmp_path)
    assert result.error is None
    assert result.applied == ()
    assert result.skipped_restart_required == ()
    assert merged is old  # untouched


def test_hot_swap_model_field_applies(tmp_path: Path) -> None:
    """Changing model field in new profile applies."""
    from opencomputer.agent.config_hot_swap import (
        _load_profile_config,
        hot_swap_config,
    )

    # Build an "old" config rooted at the same profile so memory paths
    # match (eliminating the auto-applied "memory" delta from the
    # set_profile field factories).
    old = _load_profile_config(tmp_path)

    yaml_body = (
        "model:\n"
        "  provider: openai\n"
        "  model: gpt-5\n"
    )
    _write_yaml(tmp_path / "config.yaml", yaml_body)

    merged, result = hot_swap_config(old, tmp_path)
    assert result.error is None
    assert "model" in result.applied
    assert merged.model.provider == "openai"
    assert merged.model.model == "gpt-5"
    # Other fields preserved.
    assert merged.loop == old.loop


def test_non_allowlisted_field_change_is_reported_skipped(tmp_path: Path) -> None:
    """Loop config delta → restart-required (not in allowlist)."""
    from opencomputer.agent.config_hot_swap import (
        _load_profile_config,
        hot_swap_config,
    )

    old = _load_profile_config(tmp_path)
    yaml_body = "loop:\n  max_iterations: 99\n"
    _write_yaml(tmp_path / "config.yaml", yaml_body)

    merged, result = hot_swap_config(old, tmp_path)
    assert result.error is None
    # loop field changed but is NOT in allowlist → skipped
    assert "loop" in result.skipped_restart_required
    assert merged.loop.max_iterations == old.loop.max_iterations  # NOT applied


def test_mcp_change_is_restart_required(tmp_path: Path) -> None:
    """MCP config differences are NOT hot-swapped here (§9.5 handles it)."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_hot_swap import (
        HOT_SWAPPABLE_TOP_LEVEL_FIELDS,
    )

    # The allowlist must NOT include "mcp" — that's the §9.5 contract.
    assert "mcp" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "loop" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "tools" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "hooks" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS


def test_field_delta_detector_finds_top_level_changes() -> None:
    import dataclasses

    from opencomputer.agent.config import (
        Config,
        ModelConfig,
        default_config,
    )
    from opencomputer.agent.config_hot_swap import compute_field_deltas

    a = default_config()
    b = dataclasses.replace(
        a, model=ModelConfig(provider="openai", model="gpt-5"),
    )

    deltas = compute_field_deltas(a, b)
    assert deltas["model"] is True
    assert deltas["loop"] is False
    assert deltas["memory"] is False


def test_hot_swap_rejects_bad_args() -> None:
    from opencomputer.agent.config import default_config
    from opencomputer.agent.config_hot_swap import hot_swap_config

    with pytest.raises(TypeError, match="Config"):
        hot_swap_config("not a config", Path("/x"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Path"):
        hot_swap_config(default_config(), "not a path")  # type: ignore[arg-type]


def test_allowlist_contains_documented_fields() -> None:
    """Sanity-check the allowlist matches the documented production policy."""
    from opencomputer.agent.config_hot_swap import HOT_SWAPPABLE_TOP_LEVEL_FIELDS

    # Must include the high-leverage hot fields.
    assert "model" in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "memory" in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    # Must exclude the dangerous fields.
    assert "loop" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "mcp" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "tools" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS
    assert "hooks" not in HOT_SWAPPABLE_TOP_LEVEL_FIELDS


def test_hot_swap_handles_loader_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the loader raises, return error + keep old config."""
    from opencomputer.agent import config_hot_swap as mod
    from opencomputer.agent.config import default_config

    old = default_config()

    def _boom(_home: Path) -> Any:
        raise RuntimeError("config load broke")

    monkeypatch.setattr(mod, "_load_profile_config", _boom)

    merged, result = mod.hot_swap_config(old, tmp_path)
    assert result.error is not None
    assert "config load broke" in result.error
    assert merged is old
