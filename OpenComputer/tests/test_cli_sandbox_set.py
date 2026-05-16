"""CLI tests for the Milestone-2 ``oc sandbox set`` + extended ``explain``.

M2 task T2.7. ``oc sandbox set`` persists the ``backend`` / ``scope`` /
``fallback`` keys of the active profile's ``config.yaml`` ``sandbox:``
block; the bare ``oc sandbox explain`` is extended to surface the
configured backend, its availability, the fallback policy, and a
one-line "what a tool call resolves to" summary.

Each test re-roots the profile home at a ``tmp_path`` via ``set_profile``
so the CLI writes a throwaway ``config.yaml``.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from opencomputer.agent.config_store import load_config
from opencomputer.cli_sandbox import sandbox_app
from opencomputer.sandbox.policy import (
    SANDBOX_FALLBACK_ERROR,
    SANDBOX_FALLBACK_LOCAL,
    SandboxPolicy,
    SandboxScope,
)
from plugin_sdk.profile_context import set_profile

runner = CliRunner()


# ─── oc sandbox set — happy paths ──────────────────────────────────────


def test_set_backend_persists_to_config(tmp_path: Path) -> None:
    """``set --backend e2b`` writes sandbox.backend and round-trips."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--backend", "e2b"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.backend == "e2b"
    assert "sandbox config updated" in result.output


def test_set_scope_persists_to_config(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--scope", "session"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.scope is SandboxScope.SESSION


def test_set_fallback_persists_to_config(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--fallback", "local"])
        assert result.exit_code == 0, result.output
        assert load_config().sandbox.fallback == SANDBOX_FALLBACK_LOCAL


def test_set_all_three_keys_at_once(tmp_path: Path) -> None:
    """All three flags in one call land all three keys."""
    with set_profile(tmp_path):
        result = runner.invoke(
            sandbox_app,
            ["set", "--backend", "docker", "--scope", "agent", "--fallback", "error"],
        )
        assert result.exit_code == 0, result.output
        pol = load_config().sandbox
    assert pol.backend == "docker"
    assert pol.scope is SandboxScope.AGENT
    assert pol.fallback == SANDBOX_FALLBACK_ERROR


def test_set_writes_exactly_the_expected_sandbox_keys(tmp_path: Path) -> None:
    """The persisted ``sandbox:`` block carries exactly backend + scope.

    ``fallback=error`` is the default and ``SandboxPolicy.to_mapping``
    omits default-valued fields, so a set of backend+scope must produce
    a block with ``backend`` and ``scope`` and *no* ``fallback`` key.
    """
    import yaml

    with set_profile(tmp_path):
        result = runner.invoke(
            sandbox_app, ["set", "--backend", "e2b", "--scope", "session"]
        )
        assert result.exit_code == 0, result.output
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    block = raw["sandbox"]
    assert block["backend"] == "e2b"
    assert block["scope"] == "session"
    assert "fallback" not in block  # default-valued → omitted


def test_set_partial_preserves_other_keys(tmp_path: Path) -> None:
    """A second ``set`` touching only --scope must not drop the earlier backend."""
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["set", "--backend", "e2b", "--fallback", "local"])
        result = runner.invoke(sandbox_app, ["set", "--scope", "shared"])
        assert result.exit_code == 0, result.output
        pol = load_config().sandbox
    assert pol.backend == "e2b"  # preserved
    assert pol.fallback == SANDBOX_FALLBACK_LOCAL  # preserved
    assert pol.scope is SandboxScope.SHARED  # changed


def test_set_preserves_existing_tool_lists(tmp_path: Path) -> None:
    """Setting a backend must not drop a hand-configured tools.deny list."""
    (tmp_path / "config.yaml").write_text(
        "sandbox:\n  scope: tool\n  tools:\n    deny: [Bash]\n"
    )
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--backend", "docker"])
        assert result.exit_code == 0, result.output
        pol = load_config().sandbox
    assert pol.backend == "docker"
    assert pol.tools_deny == ("Bash",)
    assert pol.scope is SandboxScope.TOOL  # untouched


def test_set_round_trips_through_config(tmp_path: Path) -> None:
    """``set`` then a fresh ``load_config`` reconstructs the exact policy."""
    with set_profile(tmp_path):
        runner.invoke(
            sandbox_app,
            ["set", "--backend", "e2b", "--scope", "session", "--fallback", "local"],
        )
        pol = load_config().sandbox
    assert pol == SandboxPolicy(
        scope=SandboxScope.SESSION,
        backend="e2b",
        fallback=SANDBOX_FALLBACK_LOCAL,
    )


# ─── oc sandbox set — rejection paths (trust-boundary validation) ──────


def test_set_rejects_unknown_backend(tmp_path: Path) -> None:
    """An unknown --backend is rejected with exit 2 and writes nothing."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--backend", "bogus-backend"])
        assert result.exit_code == 2
        assert "bogus-backend" in result.output
        # Nothing persisted — config.yaml not even created.
        assert not (tmp_path / "config.yaml").exists()


def test_set_rejects_auto_as_backend(tmp_path: Path) -> None:
    """``auto`` is not a persistable sandbox.backend — the resolver wants a
    concrete strategy and ``_named_strategy`` rejects ``auto``."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--backend", "auto"])
    assert result.exit_code == 2
    assert "auto" in result.output


def test_set_rejects_unknown_scope(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--scope", "bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_set_rejects_unknown_fallback(tmp_path: Path) -> None:
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set", "--fallback", "silent"])
    assert result.exit_code == 2
    assert "silent" in result.output


def test_set_rejects_no_flags(tmp_path: Path) -> None:
    """``set`` with no flag is an error — nothing to do."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["set"])
    assert result.exit_code == 2
    assert "at least one" in result.output


def test_set_accepts_every_concrete_backend(tmp_path: Path) -> None:
    """Each concrete strategy name is an accepted --backend value."""
    for name in ("macos_sandbox_exec", "linux_bwrap", "docker", "ssh", "e2b", "none"):
        with set_profile(tmp_path):
            result = runner.invoke(sandbox_app, ["set", "--backend", name])
            assert result.exit_code == 0, f"{name}: {result.output}"
            assert load_config().sandbox.backend == name


# ─── oc sandbox explain — M2 extension ─────────────────────────────────


def test_explain_shows_unset_backend_by_default(tmp_path: Path) -> None:
    """With no backend configured, ``explain`` says sandboxing is not opted into."""
    with set_profile(tmp_path):
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "backend" in result.output
    assert "not opted into" in result.output
    # The resolution summary reports "no sandbox".
    assert "no sandbox" in result.output


def test_explain_shows_configured_backend(tmp_path: Path) -> None:
    """After ``set --backend e2b``, ``explain`` surfaces the backend name."""
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["set", "--backend", "e2b"])
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "e2b" in result.output
    # e2b is not installed in CI → reported unreachable.
    assert "unreachable" in result.output or "unavailable" in result.output


def test_explain_shows_fallback_policy(tmp_path: Path) -> None:
    """``explain`` renders the configured fallback policy + its meaning."""
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["set", "--backend", "e2b", "--fallback", "local"])
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "fallback" in result.output
    assert "local" in result.output
    assert "WARNING" in result.output  # the local-fallback description


def test_explain_resolution_summary_for_available_backend(tmp_path: Path) -> None:
    """``explain`` resolution line names the backend when it is available.

    ``none`` is always available (``NoneSandboxStrategy``), so configuring
    it as the backend gives a deterministic "routes through it" summary.
    """
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["set", "--backend", "none"])
        result = runner.invoke(sandbox_app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "resolves to" in result.output
    assert "none" in result.output


def test_explain_with_argv_still_dry_runs(tmp_path: Path) -> None:
    """The M2 extension must not break ``explain -- <argv>`` dry-run mode."""
    with set_profile(tmp_path):
        runner.invoke(sandbox_app, ["set", "--backend", "docker"])
        result = runner.invoke(sandbox_app, ["explain", "--", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output
