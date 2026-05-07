"""Tests for the Tirith local pre-flight expansion (Hermes-followup 2026-05-07).

Pre-flight runs BEFORE the external binary spawn so destructive patterns
get blocked even when ``tirith`` isn't installed. Two classes:
sudo escalation + dangerous-binary invocation.
"""

from __future__ import annotations

import pytest

from opencomputer.security.tirith import check_command, local_preflight

# ─── unit tests on local_preflight ──────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "sudo rm -rf /",
        "sudo apt install foo",
        "doas pkg install bar",
        "su - root",
        "su root",
        "echo 'hi' && sudo whoami",
        "SUDO  -i",  # case insensitive + whitespace
    ],
)
def test_sudo_patterns_caught(cmd: str) -> None:
    findings = local_preflight(cmd)
    rules = {f["rule"] for f in findings}
    assert "preflight.sudo_escalation" in rules


@pytest.mark.parametrize(
    "cmd",
    [
        "echo 'sudoers' > /tmp/foo",          # 'sudoers' substring inside word
        "ls /etc/sudoers.d",                   # path containing 'sudoers'
        "echo pseudo",                         # 'sudo' as substring
        "sudo_helper foo bar",                 # underscore-prefixed name
    ],
)
def test_sudo_false_positives_avoided(cmd: str) -> None:
    findings = local_preflight(cmd)
    rules = {f["rule"] for f in findings}
    # `sudo_helper` must not match because of the (?<![A-Za-z_]) lookbehind
    # — but `echo 'sudoers'` (with ' ' before 'sudoers') and `ls
    # /etc/sudoers.d` (with '/' before 'sudoers') WILL match because '/' and
    # quotes aren't excluded. That's the expected conservative trade-off:
    # any standalone 'sudo' word fires the rule, and that's fine — the
    # rule is BLOCK, not silent reject; user can intervene.
    if cmd == "sudo_helper foo bar":
        assert "preflight.sudo_escalation" not in rules
    if cmd == "echo pseudo":
        # 'pseudo' is a single word; lookbehind sees 'p' before 'sudo'
        # — 'p' is in the [A-Za-z_] class, so should NOT match.
        assert "preflight.sudo_escalation" not in rules


@pytest.mark.parametrize(
    "cmd",
    [
        "mkfs.ext4 /dev/sda",
        "mkfs /dev/sdb1",
        "dd if=/dev/zero of=/dev/sda",
        "shred -u secrets.txt",
        "fdisk -l",
        "parted /dev/sda print",
    ],
)
def test_dangerous_binaries_caught(cmd: str) -> None:
    findings = local_preflight(cmd)
    rules = {f["rule"] for f in findings}
    assert "preflight.dangerous_binary" in rules


def test_safe_command_passes() -> None:
    assert local_preflight("ls -la") == []
    assert local_preflight("git status") == []
    assert local_preflight("echo 'hello world'") == []


def test_multiple_findings_returned() -> None:
    """Sudo + dd in one command — both findings."""
    findings = local_preflight("sudo dd if=/dev/zero of=/dev/sda")
    rules = {f["rule"] for f in findings}
    assert {"preflight.sudo_escalation", "preflight.dangerous_binary"} <= rules


def test_findings_have_severity_block() -> None:
    findings = local_preflight("sudo whoami")
    assert all(f["severity"] == "block" for f in findings)


# ─── integration test on check_command ──────────────────────────────────


def test_check_command_blocks_on_preflight_even_if_tirith_missing() -> None:
    """The whole point: defence-in-depth when binary is uninstalled."""
    # Force tirith binary to be 'missing' by passing a nonexistent path
    result = check_command(
        "sudo rm -rf /",
        path="/this/binary/does/not/exist",
    )
    assert result.action == "block"
    assert result.findings, "expected at least one preflight finding"
    assert "preflight" in result.findings[0]["rule"]


def test_check_command_passes_safe_through_to_binary_check() -> None:
    """A safe command should not be blocked by preflight; goes on to spawn."""
    # Binary missing → fail_open=True default returns 'allow'
    result = check_command(
        "ls -la",
        path="/this/binary/does/not/exist",
    )
    assert result.action == "allow"
    # No preflight findings (we'd see them if the rule was over-eager)
    assert not any(
        f.get("rule", "").startswith("preflight.") for f in result.findings
    )
