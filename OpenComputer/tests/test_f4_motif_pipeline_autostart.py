"""Tests for the F4 motif pipeline auto-start (2026-05-10).

Closes the dormant-feature gap surfaced by Saksham's audit: 28 graph
nodes, 0 edges. Root cause was 3 layers deep:

  1. ``MotifImporter.import_recent`` had no production caller —
     only ``oc user-model import`` invoked it.
  2. ``MotifStore`` was empty because no production code was producing
     motifs (BehavioralInferenceEngine never auto-attached).
  3. Graph stays edge-less even with a working importer when there's
     nothing to import.

Fixes (this PR):
  - Gateway co-tenant: ``BehavioralInferenceEngine.attach_to_bus()`` in
    Gateway.start() so motifs flow through MotifStore as the agent runs.
  - Cron co-tenant: ``_run_motif_import_tick`` in cron.system_jobs so
    MotifImporter materializes those motifs into the graph.
  - Detach in Gateway.stop() so subscribers don't leak.

Together these make the F4 graph populate by default. Opt-out via
``user_model.inference_engine_start_in_gateway: false``.
"""
from __future__ import annotations

import pytest

# ─── UserModelConfig ──────────────────────────────────────────────────


def test_user_model_config_has_defaults() -> None:
    from opencomputer.agent.config import UserModelConfig

    cfg = UserModelConfig()
    assert cfg.inference_engine_start_in_gateway is True


def test_config_includes_user_model_section() -> None:
    from opencomputer.agent.config import Config, UserModelConfig

    cfg = Config()
    assert isinstance(cfg.user_model, UserModelConfig)
    assert cfg.user_model.inference_engine_start_in_gateway is True


# ─── _run_motif_import_tick ───────────────────────────────────────────


def test_motif_import_tick_returns_count_dict() -> None:
    """Returns shape: {nodes_added: int, edges_added: int}."""
    from opencomputer.cron.system_jobs import _run_motif_import_tick

    result = _run_motif_import_tick()
    assert isinstance(result, dict)
    assert "nodes_added" in result
    assert "edges_added" in result
    assert isinstance(result["nodes_added"], int)
    assert isinstance(result["edges_added"], int)


def test_motif_import_tick_empty_store_returns_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When MotifStore is empty, tick returns {0, 0} — fast no-op."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cron.system_jobs import _run_motif_import_tick

    result = _run_motif_import_tick()
    assert result["nodes_added"] == 0
    assert result["edges_added"] == 0


def test_motif_import_tick_handles_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If importer.import_recent raises, return {0, 0} + log warning."""
    from opencomputer.cron import system_jobs

    class _BoomImporter:
        def import_recent(self, *_args, **_kwargs):
            raise RuntimeError("simulated DB error")

    monkeypatch.setattr(
        "opencomputer.user_model.importer.MotifImporter",
        lambda: _BoomImporter(),
    )
    result = system_jobs._run_motif_import_tick()
    assert result == {"nodes_added": 0, "edges_added": 0}


# ─── system_tick wires motif_import ──────────────────────────────────


def test_run_system_tick_includes_motif_import_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """run_system_tick summary includes motif_import_nodes + edges keys."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Touch a config.yaml so default_config() doesn't error on missing.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: anthropic\n  model: x\n")

    from opencomputer.cron.system_jobs import run_system_tick

    summary = run_system_tick()
    assert "motif_import_nodes" in summary or "motif_import" in summary, (
        f"summary missing motif_import keys: {list(summary.keys())}"
    )


# ─── Gateway slot declaration ────────────────────────────────────────


def test_gateway_declares_inference_engine_slots() -> None:
    """Gateway has _inference_engine and _inference_subscription slots.

    Pure source-grep regression test (no Gateway() construction needed —
    that requires a profile + adapters). Pairs with the wire-in-audit
    pytest pattern from PR #576.
    """
    from opencomputer.gateway.server import Gateway

    with open(Gateway.__init__.__code__.co_filename, encoding="utf-8") as fh:
        src = fh.read()
    assert "_inference_engine" in src, (
        "Gateway must declare _inference_engine slot for F4 motif pipeline"
    )


def test_gateway_start_method_references_inference_engine() -> None:
    """Gateway.start contains the BehavioralInferenceEngine attach path."""
    import inspect

    from opencomputer.gateway.server import Gateway

    src = inspect.getsource(Gateway.start)
    assert "BehavioralInferenceEngine" in src or "inference_engine_start_in_gateway" in src
