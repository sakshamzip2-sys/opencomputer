"""Phase gap-closure §3.5: doctor --fix contribution surface.

Plugins register HealthContribution objects; doctor runs them after the
built-in checks. When the user passes --fix, each contribution gets
`fix=True` and is expected to repair state in place. Source: openclaw
src/flows/doctor-health-contributions.ts.

These tests exercise the plumbing end-to-end without spinning up actual
plugins — they register contributions directly on the shared
PluginRegistry singleton and assert that:
- check-only mode reports the problem but does NOT mutate
- --fix mode invokes the contribution with fix=True and marks it repaired
- a broken contribution becomes a fail but does not crash the whole run
"""

from __future__ import annotations

import pytest

from opencomputer.doctor import _run_contributions
from plugin_sdk.doctor import HealthContribution, RepairResult


@pytest.fixture(autouse=True)
def _clean_contributions():
    """Each test gets a clean contributions list (shared singleton)."""
    from opencomputer.plugins.registry import registry as plugin_registry

    saved = list(plugin_registry.doctor_contributions)
    plugin_registry.doctor_contributions.clear()
    try:
        yield
    finally:
        plugin_registry.doctor_contributions.clear()
        plugin_registry.doctor_contributions.extend(saved)


async def test_contribution_runs_and_result_is_surfaced() -> None:
    from opencomputer.plugins.registry import registry as plugin_registry

    async def contrib(fix: bool) -> RepairResult:
        return RepairResult(
            id="check-x",
            status="pass",
            detail=f"fix={fix}",
        )

    plugin_registry.doctor_contributions.append(
        HealthContribution(id="check-x", description="test", run=contrib)
    )
    checks = await _run_contributions(fix=False)
    assert len(checks) == 1
    assert checks[0].name == "check-x"
    assert checks[0].status == "pass"
    assert checks[0].detail == "fix=False"


async def test_fix_true_is_forwarded_to_contribution() -> None:
    from opencomputer.plugins.registry import registry as plugin_registry

    received: list[bool] = []

    async def contrib(fix: bool) -> RepairResult:
        received.append(fix)
        return RepairResult(
            id="check-y",
            status="pass",
            repaired=fix,
            detail="ok",
        )

    plugin_registry.doctor_contributions.append(
        HealthContribution(id="check-y", description="test", run=contrib)
    )

    # Check-only: no repair
    checks_readonly = await _run_contributions(fix=False)
    # Fix mode: repair invoked
    checks_fix = await _run_contributions(fix=True)

    assert received == [False, True]
    assert "[repaired]" not in checks_readonly[0].detail
    assert "[repaired]" in checks_fix[0].detail


async def test_broken_contribution_becomes_fail_not_crash() -> None:
    from opencomputer.plugins.registry import registry as plugin_registry

    async def bad(fix: bool) -> RepairResult:
        raise RuntimeError("plugin author forgot something")

    async def good(fix: bool) -> RepairResult:
        return RepairResult(id="good", status="pass")

    plugin_registry.doctor_contributions.append(
        HealthContribution(id="bad", description="bad contrib", run=bad)
    )
    plugin_registry.doctor_contributions.append(
        HealthContribution(id="good", description="good contrib", run=good)
    )

    checks = await _run_contributions(fix=False)
    by_id = {c.name: c for c in checks}
    assert by_id["bad"].status == "fail"
    assert "RuntimeError" in by_id["bad"].detail
    # The good contribution still ran — a broken contribution cannot take
    # the whole doctor run down.
    assert by_id["good"].status == "pass"


async def test_contribution_can_repair_a_simulated_legacy_config(
    tmp_path, monkeypatch
) -> None:
    """Representative of the actual repair pattern: the contribution owns
    the legacy shape and rewrites it to the new shape when fix=True."""
    from opencomputer.plugins.registry import registry as plugin_registry

    legacy_file = tmp_path / "legacy.yaml"
    legacy_file.write_text("version: 0\nold_field: abc\n")

    async def migrate(fix: bool) -> RepairResult:
        txt = legacy_file.read_text()
        if "old_field" not in txt:
            return RepairResult(id="legacy-migrate", status="pass", detail="up to date")
        if not fix:
            return RepairResult(
                id="legacy-migrate",
                status="warn",
                detail="legacy shape — run doctor --fix to migrate",
            )
        legacy_file.write_text(txt.replace("old_field", "new_field"))
        return RepairResult(
            id="legacy-migrate",
            status="pass",
            detail="migrated old_field → new_field",
            repaired=True,
        )

    plugin_registry.doctor_contributions.append(
        HealthContribution(id="legacy-migrate", description="legacy shape", run=migrate)
    )

    # Check-only: file untouched, warn
    checks = await _run_contributions(fix=False)
    assert checks[0].status == "warn"
    assert "old_field" in legacy_file.read_text()

    # Fix mode: file rewritten, pass + [repaired]
    checks = await _run_contributions(fix=True)
    assert checks[0].status == "pass"
    assert "[repaired]" in checks[0].detail
    assert "new_field" in legacy_file.read_text()
    assert "old_field" not in legacy_file.read_text()
