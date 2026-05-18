"""Wiring tests for the activation planner (best-of-three Recipe 3).

``plan_activations`` itself is covered by ``test_activation_planner.py``;
this file covers how it is wired into ``cli._discover_plugins`` — the
flag gate, the escape hatch, and the narrowed-set builder. The planner
ships flag-gated and OFF by default, so behaviour is byte-identical to
pre-Recipe-3 until ``OPENCOMPUTER_PLUGIN_ACTIVATION=plan`` is set.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.cli import (
    _activation_mode,
    _activation_narrowed_enabled_ids,
)

# ── flag resolution ──────────────────────────────────────────────────


def test_mode_defaults_to_all_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_PLUGIN_ACTIVATION", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", raising=False)
    assert _activation_mode() == "all"


def test_mode_plan_when_flag_set(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    assert _activation_mode() == "plan"


def test_mode_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "  PLAN ")
    assert _activation_mode() == "plan"


def test_escape_hatch_forces_all_over_plan(monkeypatch) -> None:
    """OPENCOMPUTER_LOAD_ALL_PLUGINS=1 wins even when plan is requested."""
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")
    monkeypatch.setenv("OPENCOMPUTER_LOAD_ALL_PLUGINS", "1")
    assert _activation_mode() == "all"


# ── narrowed-set builder ─────────────────────────────────────────────


def test_narrowed_ids_is_a_frozenset(tmp_path: Path) -> None:
    """An empty search path yields an empty — not None — frozenset:
    discovery succeeded, it just found nothing."""
    result = _activation_narrowed_enabled_ids([tmp_path])
    assert isinstance(result, frozenset)


def test_narrowed_ids_subset_of_real_catalog() -> None:
    """Run against the real extension catalog: the planner result must
    be a subset of the discovered plugin ids (it never invents ids)."""
    from opencomputer.plugins.discovery import (
        discover,
        standard_search_paths,
    )

    search_paths = standard_search_paths()
    narrowed = _activation_narrowed_enabled_ids(search_paths)
    assert narrowed is not None
    all_ids = {c.manifest.id for c in discover(search_paths)}
    assert narrowed <= all_ids


def test_narrowed_ids_never_raises_on_bad_path() -> None:
    """A nonexistent search path must return a value, never raise —
    narrowing failure falls back to load-everything, not a crash."""
    result = _activation_narrowed_enabled_ids(
        [Path("/no/such/dir/xyz123")]
    )
    assert result == frozenset()
