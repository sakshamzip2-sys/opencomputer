"""Validate the macOS LaunchAgent template + installer scripts.

Pinned to the v2026.4.26 deployment-mode gap analysis: OC's primary
surface is Telegram (per CLAUDE.md user-prefs) and a laptop reboot
shouldn't kill the gateway. Standard macOS pattern is a LaunchAgent;
this test ensures the template stays valid XML and the installer
shell doesn't drift into broken syntax across refactors.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO_ROOT / "scripts" / "launchd"
TEMPLATE = LAUNCHD_DIR / "com.opencomputer.gateway.plist.template"
INSTALL_SH = LAUNCHD_DIR / "install.sh"
UNINSTALL_SH = LAUNCHD_DIR / "uninstall.sh"


def test_template_exists_and_is_valid_xml() -> None:
    """The plist must parse as XML even with placeholders unfilled —
    placeholders are XML-safe by design (no ``<`` / ``>``)."""
    assert TEMPLATE.exists(), f"missing {TEMPLATE}"
    raw = TEMPLATE.read_text()

    # Substitute placeholders with sane values BEFORE parsing — the
    # raw template uses ``{{...}}`` which is text content, not markup,
    # so XML parsing succeeds either way; we substitute so the test
    # also asserts the placeholders survive untouched into the rendered
    # form.
    rendered = (
        raw.replace("{{OPENCOMPUTER_BIN}}", "/usr/local/bin/opencomputer")
        .replace("{{HOME}}", "/Users/test-user")
    )
    tree = ET.fromstring(rendered)
    assert tree.tag == "plist"


def test_template_carries_load_bearing_keys() -> None:
    """Regression guard: someone refactoring the plist must not drop
    KeepAlive / RunAtLoad / ThrottleInterval, otherwise restart-on-crash
    or boot-load semantics silently break."""
    raw = TEMPLATE.read_text()
    for required in (
        "<key>Label</key>",
        "<string>com.opencomputer.gateway</string>",
        "<key>RunAtLoad</key>",
        "<key>KeepAlive</key>",
        "<key>ThrottleInterval</key>",
        "<integer>60</integer>",
        "<key>ProgramArguments</key>",
        "{{OPENCOMPUTER_BIN}}",
        "{{HOME}}",
    ):
        assert required in raw, f"missing required key/value: {required}"


def test_install_sh_syntax_is_valid() -> None:
    """``bash -n`` parse-only check — catches typos before they hit a user."""
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"install.sh has syntax errors:\n{result.stderr}"
    )


def test_uninstall_sh_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(UNINSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_uses_set_euo_pipefail() -> None:
    """Defence-in-depth: a shell installer without ``set -euo pipefail``
    silently swallows errors and leaves users with a half-installed agent."""
    src = INSTALL_SH.read_text()
    assert "set -euo pipefail" in src


def test_install_sh_refuses_on_non_macos() -> None:
    """LaunchAgents are an Apple primitive; running on Linux/WSL must
    fail loud, not silently write a useless plist."""
    src = INSTALL_SH.read_text()
    assert "Darwin" in src or "darwin" in src
    assert "uname" in src


def test_install_sh_resolves_opencomputer_absolute_path() -> None:
    """LaunchAgent's PATH is sparse — the plist MUST hold an absolute
    binary path, not the bare ``opencomputer`` name. The installer
    resolves via ``command -v`` at install time."""
    src = INSTALL_SH.read_text()
    assert 'command -v opencomputer' in src
    assert "{{OPENCOMPUTER_BIN}}" in src
    assert "OC_BIN" in src


@pytest.mark.skipif(
    not shutil.which("xmllint"),
    reason="xmllint not installed; the install.sh runs the same parse via launchctl",
)
def test_template_passes_xmllint() -> None:
    """When xmllint is available, validate the raw template too."""
    result = subprocess.run(
        ["xmllint", "--noout", str(TEMPLATE)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_dry_run_renders_template(tmp_path: Path) -> None:
    """Black-box: run install.sh --dry-run and assert the rendered output
    has both placeholders substituted."""
    if shutil.which("uname") and subprocess.check_output(["uname"]).strip() != b"Darwin":
        pytest.skip("install.sh's uname guard refuses on non-macOS hosts")
    fake_bin = tmp_path / "opencomputer"
    fake_bin.write_text("#!/bin/sh\necho fake")
    fake_bin.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "{{OPENCOMPUTER_BIN}}" not in out, "placeholder leaked into rendered output"
    assert "{{HOME}}" not in out, "placeholder leaked into rendered output"
    assert str(fake_bin) in out
    assert str(tmp_path) in out
