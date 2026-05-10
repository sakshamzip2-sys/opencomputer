"""Tests for ``opencomputer.gateway.preflight`` — channel ownership enforcement."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.gateway import preflight
from opencomputer.gateway.preflight import (
    ChannelOwnershipConflict,
    Competitor,
    detect_competitors,
    run_preflight,
    takeover,
)

# ─── _ps_snapshot helpers ────────────────────────────────────────────


def _fake_ps_output(rows: list[tuple[int, str]]) -> str:
    """Return a ps-like string given (pid, args) tuples."""
    header = "  PID COMMAND"
    body = "\n".join(f"{pid:6d} {args}" for pid, args in rows)
    return header + "\n" + body + "\n"


# ─── detect_competitors ──────────────────────────────────────────────


def test_detects_claude_code_telegram_bridge() -> None:
    fake_ps = _fake_ps_output([
        (1234, "/Users/saksham/.bun/bin/bun server.ts"),  # generic — won't match
        (5678, "bun run --cwd /Users/saksham/.claude/plugins/cache/"
               "claude-plugins-official/telegram/0.0.6 --silent start"),
        (9012, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors(exclude_pids={99999})
    assert len(comps) == 1
    assert comps[0].pid == 5678
    assert comps[0].kind == "claude_code_telegram_bridge"


def test_detects_hermes_gateway() -> None:
    fake_ps = _fake_ps_output([
        (1806, "python -m hermes_cli.main gateway run --replace"),
        (3000, "python my_random_app.py"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors(exclude_pids={99999})
    assert [c.pid for c in comps] == [1806]
    assert comps[0].kind == "hermes_gateway"


def test_detects_rival_oc_gateway() -> None:
    fake_ps = _fake_ps_output([
        (4242, "/Users/foo/.local/bin/oc --headless --profile default gateway"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors(exclude_pids={99999})
    assert len(comps) == 1
    assert comps[0].kind == "rival_oc_gateway"


def test_excludes_self_and_parent_pid() -> None:
    """Even without explicit exclude_pids, our own PID should never appear."""
    import os
    fake_ps = _fake_ps_output([
        (os.getpid(), "/some/path/oc --headless --profile default gateway"),
        (os.getppid(), "/some/path/oc --headless --profile default gateway"),
        (12345, "python -m hermes_cli.main gateway run"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors()
    assert [c.pid for c in comps] == [12345]


def test_returns_empty_when_ps_missing() -> None:
    """No ps on PATH (sandbox) → empty competitor list, not a crash."""
    with patch.object(preflight.shutil, "which", return_value=None):
        comps = detect_competitors()
    assert comps == []


def test_handles_ps_failure_gracefully() -> None:
    """ps returns non-zero → empty list, log a warning."""
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors()
    assert comps == []


def test_skips_shell_wrappers_that_mention_patterns() -> None:
    """Regression test for the 2026-05-08 self-flag bug.

    A zsh -c diagnostic script that grep-mentions
    ``claude-plugins-official/telegram`` is NOT a competitor — it's
    just a script that happens to talk about the pattern. The
    preflight previously SIGTERM'd the user's own running shell.
    """
    fake_ps = _fake_ps_output([
        # zsh wrapper running a grep that contains the literal pattern
        (1111, "/bin/zsh -c ps aux | grep claude-plugins-official/telegram"),
        # bash wrapper running a script that mentions hermes_cli
        (2222, "/bin/bash -c kill -0 1806 || python -m hermes_cli.main gateway"),
        # Real bun bridge — should still be flagged
        (3333, "bun run --cwd /Users/saksham/.claude/plugins/cache/"
               "claude-plugins-official/telegram/0.0.6 --silent start"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors(exclude_pids={99999})
    # Only the real bun process should be flagged; shells skipped.
    assert [c.pid for c in comps] == [3333]
    assert comps[0].kind == "claude_code_telegram_bridge"


def test_skips_env_shell_wrappers() -> None:
    """``/usr/bin/env zsh -c ...`` form also skipped."""
    fake_ps = _fake_ps_output([
        (1111, "/usr/bin/env zsh -c grep claude-plugins-official/telegram"),
        (2222, "/usr/bin/env bash echo hermes_cli main gateway"),
    ])
    with patch.object(preflight.subprocess, "run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_ps
        with patch.object(preflight.shutil, "which", return_value="/bin/ps"):
            comps = detect_competitors(exclude_pids={99999})
    assert comps == []


# ─── takeover ────────────────────────────────────────────────────────


def test_takeover_signals_terminates_alive_competitor(tmp_path: Path) -> None:
    """SIGTERM is sent; if process exits within grace, no SIGKILL."""
    c = Competitor(pid=12345, kind="hermes_gateway", cmdline_preview="hermes_cli")

    signals_sent: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        signals_sent.append((pid, sig))

    alive_states = iter([True, True, False])  # alive at sig=0 check, then dies

    def fake_alive(pid: int) -> bool:
        return next(alive_states, False)

    with patch.object(preflight.os, "kill", side_effect=fake_kill), \
         patch.object(preflight, "_is_alive", side_effect=fake_alive), \
         patch.object(preflight.time, "sleep", return_value=None):
        survivors = takeover([c], grace_seconds=5.0, audit_log=tmp_path / "audit.jsonl")

    assert survivors == []
    assert (12345, 15) in signals_sent  # SIGTERM
    assert (12345, 9) not in signals_sent  # NOT escalated to SIGKILL
    # Audit log written
    audit = (tmp_path / "audit.jsonl").read_text()
    rec = json.loads(audit.strip().splitlines()[0])
    assert rec["pid"] == 12345
    assert rec["signal"] == "SIGTERM"
    assert rec["exit_code"] == "clean_sigterm"


def test_takeover_escalates_to_sigkill_after_grace(tmp_path: Path) -> None:
    """Process refuses SIGTERM → SIGKILL after grace window."""
    c = Competitor(pid=12345, kind="hermes_gateway", cmdline_preview="hermes")

    signals_sent: list[tuple[int, int]] = []
    fake_time = iter([0.0, 0.1, 0.2, 5.5, 5.6, 5.7, 5.8])

    def fake_kill(pid: int, sig: int) -> None:
        signals_sent.append((pid, sig))

    def fake_monotonic() -> float:
        return next(fake_time, 999.0)

    with patch.object(preflight.os, "kill", side_effect=fake_kill), \
         patch.object(preflight, "_is_alive") as ma, \
         patch.object(preflight.time, "sleep", return_value=None), \
         patch.object(preflight.time, "monotonic", side_effect=fake_monotonic):
        # Always alive during grace window; dies after SIGKILL.
        ma.side_effect = [True, True, True, True, True, False]
        survivors = takeover([c], grace_seconds=5.0, audit_log=tmp_path / "audit.jsonl")

    assert survivors == []
    sigs = [s for (_, s) in signals_sent]
    assert 15 in sigs  # SIGTERM
    assert 9 in sigs  # SIGKILL escalation


def test_takeover_records_already_dead_competitor(tmp_path: Path) -> None:
    """Process dies BEFORE we send SIGTERM (race) → recorded gracefully."""
    c = Competitor(pid=12345, kind="hermes_gateway", cmdline_preview="hermes")

    with patch.object(preflight, "_is_alive", return_value=False):
        survivors = takeover([c], audit_log=tmp_path / "audit.jsonl")

    assert survivors == []
    rec = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert rec["signal"] == "already_dead"


def test_takeover_skips_audit_when_path_none() -> None:
    """audit_log=None must NOT crash; no file written."""
    c = Competitor(pid=12345, kind="hermes_gateway", cmdline_preview="hermes")
    with patch.object(preflight, "_is_alive", return_value=False):
        survivors = takeover([c], audit_log=None)
    assert survivors == []


# ─── run_preflight ────────────────────────────────────────────────────


def test_run_preflight_returns_empty_when_no_competitors() -> None:
    with patch.object(preflight, "detect_competitors", return_value=[]):
        survivors = run_preflight(takeover_on_start=True)
    assert survivors == []


def test_run_preflight_raises_when_competitors_and_takeover_disabled() -> None:
    """The default-safe behavior: refuse to start, name the offender."""
    fake = [Competitor(pid=1234, kind="hermes_gateway", cmdline_preview="hermes")]
    with patch.object(preflight, "detect_competitors", return_value=fake):
        with pytest.raises(ChannelOwnershipConflict) as exc_info:
            run_preflight(takeover_on_start=False)
    msg = str(exc_info.value)
    assert "channel ownership conflict" in msg
    assert "PID 1234" in msg
    assert "kill -TERM 1234" in msg
    assert "--force-takeover" in msg


def test_run_preflight_takes_over_when_enabled(tmp_path: Path) -> None:
    fake = [Competitor(pid=1234, kind="hermes_gateway", cmdline_preview="hermes")]
    with patch.object(preflight, "detect_competitors", return_value=fake), \
         patch.object(preflight, "takeover", return_value=[]) as mock_to:
        survivors = run_preflight(
            takeover_on_start=True,
            grace_seconds=3.0,
            audit_log=tmp_path / "a.jsonl",
        )
    assert survivors == []
    mock_to.assert_called_once_with(
        fake, grace_seconds=3.0, audit_log=tmp_path / "a.jsonl",
    )


def test_run_preflight_returns_survivors_on_partial_failure() -> None:
    """Takeover ran but some refused to die → survivors returned, no exception."""
    fake = [
        Competitor(pid=1234, kind="hermes_gateway", cmdline_preview="hermes"),
        Competitor(pid=5678, kind="claude_code_telegram_bridge", cmdline_preview="bun"),
    ]
    survivor = [fake[1]]
    with patch.object(preflight, "detect_competitors", return_value=fake), \
         patch.object(preflight, "takeover", return_value=survivor):
        result = run_preflight(takeover_on_start=True)
    assert result == survivor


# ─── ChannelOwnershipConflict message format ─────────────────────────


def test_conflict_message_lists_all_pids() -> None:
    comps = [
        Competitor(pid=1, kind="hermes_gateway", cmdline_preview="hermes"),
        Competitor(pid=2, kind="claude_code_telegram_bridge", cmdline_preview="bun"),
        Competitor(pid=3, kind="rival_oc_gateway", cmdline_preview="oc gateway"),
    ]
    err = ChannelOwnershipConflict(comps)
    msg = str(err)
    for pid in (1, 2, 3):
        assert f"PID {pid}" in msg
    assert "kill -TERM 1 2 3" in msg
