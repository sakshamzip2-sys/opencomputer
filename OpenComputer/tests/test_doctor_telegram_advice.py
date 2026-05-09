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
