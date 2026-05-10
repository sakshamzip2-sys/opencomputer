"""B3: doctor must include actionable `kill PID` advice when multiple
opencomputer gateway processes hold the same telegram bot token slot."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.doctor import _check_telegram_polling_conflict


def test_doctor_includes_kill_command_for_each_rogue_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given two rogue PIDs, the warning must contain `kill <PID>` for both."""
    fake_ps_output = (
        "  PID COMMAND          ARGS\n"
        " 73440 python /opt/homebrew/Cellar/python@3.11/.../opencomputer gateway\n"
        " 99999 python /Users/saksham/.local/bin/opencomputer gateway start\n"
    )

    class _FakeProc:
        returncode = 0
        stdout = fake_ps_output

    with patch("subprocess.run", return_value=_FakeProc()), \
         patch("shutil.which", return_value="/bin/ps"):
        results = _check_telegram_polling_conflict()

    assert results, "expected at least one Check"
    warn = next((r for r in results if r.status == "warn"), None)
    assert warn is not None, f"expected a warn-level Check, got: {results}"
    assert "kill 73440" in warn.detail, warn.detail
    assert "kill 99999" in warn.detail, warn.detail


def test_doctor_excludes_canonical_launchd_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launchd-managed gateway (always invoked with --headless) is the
    SUPPOSED-to-be-polling process — it must NOT be flagged as 'other'.

    Regression: PR #570 introduced the actionable kill advice but didn't
    distinguish the canonical gateway from a rogue duplicate, so even a
    healthy single-daemon setup tripped the warning permanently."""
    fake_ps_output = (
        "  PID COMMAND          ARGS\n"
        # The canonical launchd gateway — always passes --headless.
        "  6705 python /Users/saksham/.local/bin/oc --headless --profile default gateway\n"
    )

    class _FakeProc:
        returncode = 0
        stdout = fake_ps_output

    with patch("subprocess.run", return_value=_FakeProc()), \
         patch("shutil.which", return_value="/bin/ps"):
        results = _check_telegram_polling_conflict()

    # Should report 'pass' (no rogue), not 'warn'.
    assert results, "expected at least one Check"
    statuses = [r.status for r in results]
    assert "warn" not in statuses, (
        f"canonical launchd gateway falsely flagged as rogue: {results}"
    )
    pass_check = next((r for r in results if r.status == "pass"), None)
    assert pass_check is not None
    assert "no other gateway process" in pass_check.detail.lower()
