"""Best-of-three review-followup hardening — coverage-gap tests.

The pr-test-analyzer (Phase-7 review of PR #640) flagged behaviours that
work but had no regression test. These close the high-value gaps: the
``doctor --all`` CLI surface, the Tier-3 R3 flag-OFF invariant, the
update-check cache short-circuit, the marketplace trust-key render, and
the R7 indicator override-scope contract.
"""
from __future__ import annotations

import time

from typer.testing import CliRunner

from opencomputer.cli_plugin import plugin_app

runner = CliRunner()


# ── C1 — oc plugin doctor --all ──────────────────────────────────────


def test_c1_plugin_doctor_all_runs_without_crash() -> None:
    """``oc plugin doctor --all`` diagnoses every installed plugin and
    renders a table — exit 0 (all OK) or 1 (a real per-plugin failure),
    never a crash."""
    result = runner.invoke(plugin_app, ["doctor", "--all"])
    assert result.exit_code in (0, 1), (
        f"doctor --all must not crash; exit={result.exit_code} "
        f"output={result.output[:400]!r}"
    )
    assert "doctor" in result.output.lower()


# ── C3 — R3 flag-OFF invariant (planner not called) ──────────────────


def test_c3_discover_plugins_skips_planner_when_flag_off(monkeypatch) -> None:  # noqa: ANN001
    """Tier-3 R3 invariant: with ``OPENCOMPUTER_PLUGIN_ACTIVATION`` unset,
    ``_discover_plugins`` must NOT invoke the activation planner — even
    when called with ``narrow_channels=True`` (the ``oc chat`` path).
    Behaviour stays byte-identical to pre-R3."""
    from opencomputer import cli

    calls: list[int] = []
    monkeypatch.setattr(
        cli, "_activation_narrowed_enabled_ids",
        lambda *a, **k: (calls.append(1), None)[1],
    )
    monkeypatch.setattr(cli, "_resolve_plugin_filter", lambda: None)
    monkeypatch.setattr(
        type(cli.plugin_registry), "load_all", lambda self, *a, **k: []
    )
    monkeypatch.delenv("OPENCOMPUTER_PLUGIN_ACTIVATION", raising=False)

    cli._discover_plugins(narrow_channels=True)
    assert calls == [], "planner must NOT run when the flag is off"


def test_c3_discover_plugins_runs_planner_when_flag_on(monkeypatch) -> None:  # noqa: ANN001
    """With ``narrow_channels=True``, ``OPENCOMPUTER_PLUGIN_ACTIVATION=plan``
    and no explicit profile filter, the planner IS consulted (the
    cold-start channel-narrowing path for ``oc chat``)."""
    from opencomputer import cli

    calls: list[int] = []
    monkeypatch.setattr(
        cli, "_activation_narrowed_enabled_ids",
        lambda *a, **k: (calls.append(1), None)[1],
    )
    monkeypatch.setattr(cli, "_resolve_plugin_filter", lambda: None)
    monkeypatch.setattr(
        type(cli.plugin_registry), "load_all", lambda self, *a, **k: []
    )
    monkeypatch.setenv("OPENCOMPUTER_PLUGIN_ACTIVATION", "plan")

    cli._discover_plugins(narrow_channels=True)
    assert calls == [1], "planner MUST run when the flag opts in"


# ── I2 — update-check honours a fresh cache ──────────────────────────


def test_i2_update_check_honours_a_fresh_cache(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A fresh on-disk cache short-circuits ``oc plugin update-check`` —
    the 6h TTL must prevent a repeated network poll."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.plugins.update_check import cache_path, write_cache

    write_cache([], cache_path(), now=time.time())  # fresh, empty
    result = runner.invoke(plugin_app, ["update-check"])
    assert result.exit_code == 0, result.output
    assert "cached" in result.output.lower(), (
        f"a fresh cache must be honoured (no re-poll); output={result.output!r}"
    )


# ── I6 — marketplace list renders the trust-key fingerprint ──────────


def test_i6_marketplace_list_renders_trust_key(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """``oc plugin marketplace list`` shows each marketplace's recorded
    trust-key fingerprint (best-of-three R5 acceptance criterion)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.plugins.marketplaces import add_marketplace

    add_marketplace(
        "trusted-mp", "https://trusted.dev/c.json",
        trust_key="deadbeefcafe1234",
    )
    result = runner.invoke(plugin_app, ["marketplace", "list"])
    assert result.exit_code == 0, result.output
    assert "trusted-mp" in result.output
    assert "deadbeef" in result.output, (
        f"the trust-key fingerprint must render; output={result.output!r}"
    )


# ── R7 / I5 — indicator override scope contract ──────────────────────


def test_r7_indicator_override_scope_and_reset() -> None:
    """R7 — ``_INDICATOR_OVERRIDE`` is a module global: process-scoped,
    which IS session-scoped for ``oc chat`` (one process per session).
    The user-facing reset is ``/indicator skin`` — i.e.
    ``set_indicator_style("")``. This pins that contract: set, observe,
    reset. (A long-running daemon sharing one process across loops is a
    documented limitation — `set_indicator_style("")` is its reset
    primitive.)"""
    from opencomputer.cli_ui.busy_indicator import (
        current_indicator_style,
        set_indicator_style,
    )

    try:
        assert set_indicator_style("dots") is True
        assert current_indicator_style() == "dots"
        # the documented reset path — `/indicator skin`:
        assert set_indicator_style("") is True
        assert current_indicator_style() == ""
        # unknown styles are rejected, override unchanged:
        assert set_indicator_style("bogus-xyz") is False
        assert current_indicator_style() == ""
    finally:
        set_indicator_style("")
