"""Regression: ``build_production_dependencies`` applies evolution tuning.

Before 2026-05-11 the dreaming-v2 cron's ``build_production_dependencies``
read ``cfg.memory.dreaming_v2_score_threshold`` directly from the base
Config, ignoring the orchestrator's persisted tuning at
``<profile_home>/skills/evolution_tuning.json``. The orchestrator
faithfully tuned the value, but the value was never consumed.

This test pins the override-on-changed semantics:

* When the tuning value matches the default (no orchestrator change),
  config.yaml's operator-set value wins (preserves explicit overrides).
* When the tuning value differs from the default (orchestrator moved
  it), the tuning value wins (closes the loop).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.evolution_orchestrator import SCHEMA_VERSION


def _write_tuning(profile_home: Path, *, score: float, recall: int) -> None:
    skills = profile_home / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "confidence_threshold": 70,
                "dreaming_v2_score_threshold": score,
                "dreaming_v2_min_recall": recall,
                "decisions_observed": 25,
                "last_recompute_ts": 1700000000.0,
            }
        )
    )


def test_tuning_unchanged_preserves_config_yaml_values(
    tmp_path: Path, monkeypatch
):
    """Tuning at defaults → config.yaml's value is preserved.

    This is the operator-override invariant. If an operator sets a
    high score_threshold in config.yaml and the orchestrator has not
    moved its own copy off the default, the config.yaml value must
    not be silently overridden.
    """
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _write_tuning(tmp_path, score=0.65, recall=2)  # both at default

    # Patch load_config so we don't depend on the user's real config.yaml.
    from opencomputer.cron import dreaming_v2_tick as mod

    class _Cfg:
        class memory:
            dreaming_v2_enabled = False
            dreaming_v2_score_threshold = 0.85  # operator-set high
            dreaming_v2_min_recall_count = 4    # operator-set high
            dreaming_v2_diversity_threshold = 0.8
            dreaming_v2_max_promotions_per_run = 20
            dreaming_v2_dreams_md_max_bytes = 16384
            declarative_path = tmp_path / "MEMORY.md"
            skills_path = tmp_path / "skills"
            memory_char_limit = 8000

        class session:
            db_path = tmp_path / "s.db"

        class model:
            provider = "anthropic"
            model = "claude-test"
            api_mode = None

    monkeypatch.setattr(mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(mod, "_home", lambda: tmp_path)
    # Resolve_provider would hit live registry — short-circuit.
    monkeypatch.setattr(
        "opencomputer.cli._resolve_provider",
        lambda *a, **kw: None,
        raising=False,
    )

    deps = mod.build_production_dependencies(profile_home=tmp_path)

    # Tuning was at defaults; operator-set config values should win.
    assert deps.config.score_threshold == pytest.approx(0.85)
    assert deps.config.min_recall_count == 4


def test_tuning_changed_overrides_config_yaml_values(
    tmp_path: Path, monkeypatch
):
    """Tuning moved off defaults → tuning wins.

    This is the closed-loop invariant. After the orchestrator has
    moved score_threshold or min_recall off the default, the cron
    must honor the tuned value over the config.yaml value.
    """
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _write_tuning(tmp_path, score=0.50, recall=1)  # both shifted off default

    from opencomputer.cron import dreaming_v2_tick as mod

    class _Cfg:
        class memory:
            dreaming_v2_enabled = False
            # Config.yaml says something different — but tuning has moved.
            dreaming_v2_score_threshold = 0.85
            dreaming_v2_min_recall_count = 4
            dreaming_v2_diversity_threshold = 0.8
            dreaming_v2_max_promotions_per_run = 20
            dreaming_v2_dreams_md_max_bytes = 16384
            declarative_path = tmp_path / "MEMORY.md"
            skills_path = tmp_path / "skills"
            memory_char_limit = 8000

        class session:
            db_path = tmp_path / "s.db"

        class model:
            provider = "anthropic"
            model = "claude-test"
            api_mode = None

    monkeypatch.setattr(mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(
        "opencomputer.cli._resolve_provider",
        lambda *a, **kw: None,
        raising=False,
    )

    deps = mod.build_production_dependencies(profile_home=tmp_path)

    # Tuning beats config.yaml when it's been moved off defaults.
    assert deps.config.score_threshold == pytest.approx(0.50)
    assert deps.config.min_recall_count == 1


def test_missing_tuning_falls_back_to_config_yaml(tmp_path: Path, monkeypatch):
    """No tuning file → defaults assumed → config.yaml values used."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    # No file written intentionally.

    from opencomputer.cron import dreaming_v2_tick as mod

    class _Cfg:
        class memory:
            dreaming_v2_enabled = False
            dreaming_v2_score_threshold = 0.90
            dreaming_v2_min_recall_count = 5
            dreaming_v2_diversity_threshold = 0.8
            dreaming_v2_max_promotions_per_run = 20
            dreaming_v2_dreams_md_max_bytes = 16384
            declarative_path = tmp_path / "MEMORY.md"
            skills_path = tmp_path / "skills"
            memory_char_limit = 8000

        class session:
            db_path = tmp_path / "s.db"

        class model:
            provider = "anthropic"
            model = "claude-test"
            api_mode = None

    monkeypatch.setattr(mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(
        "opencomputer.cli._resolve_provider",
        lambda *a, **kw: None,
        raising=False,
    )

    deps = mod.build_production_dependencies(profile_home=tmp_path)

    assert deps.config.score_threshold == pytest.approx(0.90)
    assert deps.config.min_recall_count == 5
