"""Tests for Phase 3.E — pluggable SandboxStrategy.

The host-aware tests use ``pytest.skipif`` so the suite runs cleanly on
macOS dev machines AND Linux CI without docker/sandbox-exec/bwrap. We
mock aggressively where strategies aren't available locally.
"""

from __future__ import annotations

import asyncio
import platform
from unittest.mock import patch

import pytest

from opencomputer.sandbox import (
    DockerStrategy,
    LinuxBwrapStrategy,
    MacOSSandboxExecStrategy,
    NoneSandboxStrategy,
    auto_strategy,
    run_sandboxed,
)
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxUnavailable

# ---------------------------------------------------------------------------
# NoneSandboxStrategy — always available, no containment, runs argv directly
# ---------------------------------------------------------------------------


def test_none_strategy_is_always_available() -> None:
    s = NoneSandboxStrategy()
    assert s.is_available() is True
    assert s.name == "none"


def test_none_strategy_explain_is_argv_passthrough() -> None:
    s = NoneSandboxStrategy()
    out = s.explain(["echo", "hi"], config=SandboxConfig())
    assert out == ["echo", "hi"]


def test_none_strategy_runs_argv_directly(caplog: pytest.LogCaptureFixture) -> None:
    """Runs even without sandboxing; warning logged."""
    s = NoneSandboxStrategy()
    with caplog.at_level("WARNING", logger="opencomputer.sandbox.none"):
        result = asyncio.run(s.run(["echo", "hi"], config=SandboxConfig()))
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.strategy_name == "none"
    assert result.wrapped_command == ["echo", "hi"]
    assert any("no containment" in rec.message for rec in caplog.records)


def test_none_strategy_timeout_kills_process() -> None:
    """A sleep that exceeds the timeout returns non-zero + sentinel stderr."""
    s = NoneSandboxStrategy()
    cfg = SandboxConfig(cpu_seconds_limit=1)
    # ``sleep 5`` reliably exists across macOS/Linux. ``cpu_seconds_limit=1``
    # ensures the wait_for fires well before sleep finishes.
    result = asyncio.run(s.run(["sleep", "5"], config=cfg))
    assert result.exit_code < 0  # sentinel TIMEOUT_EXIT_CODE
    assert "timeout" in result.stderr.lower()


def test_none_strategy_filters_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only allowlisted env vars are passed through.

    We assert via a small Python snippet so the test works on any host.
    """
    monkeypatch.setenv("SANDBOX_TEST_ALLOWED", "yes")
    monkeypatch.setenv("SANDBOX_TEST_DENIED", "no")
    s = NoneSandboxStrategy()
    cfg = SandboxConfig(allowed_env_vars=("PATH", "SANDBOX_TEST_ALLOWED"))
    snippet = (
        "import os; "
        "print('A=' + os.environ.get('SANDBOX_TEST_ALLOWED', 'missing')); "
        "print('D=' + os.environ.get('SANDBOX_TEST_DENIED', 'missing'))"
    )
    result = asyncio.run(s.run(["python3", "-c", snippet], config=cfg))
    assert result.exit_code == 0, result.stderr
    assert "A=yes" in result.stdout
    assert "D=missing" in result.stdout


# ---------------------------------------------------------------------------
# MacOSSandboxExecStrategy
# ---------------------------------------------------------------------------


def test_macos_strategy_is_available_only_on_darwin() -> None:
    s = MacOSSandboxExecStrategy()
    if platform.system() == "Darwin":
        # Most macOS systems have ``sandbox-exec`` shipped; assert
        # availability mirrors the platform check (minus the rare
        # case of a stripped install).
        from shutil import which
        expected = which("sandbox-exec") is not None
        assert s.is_available() is expected
    else:
        assert s.is_available() is False


def test_macos_strategy_explain_returns_wrapped_argv() -> None:
    """``explain()`` shape correct — first token ``sandbox-exec``, profile next."""
    s = MacOSSandboxExecStrategy()
    out = s.explain(["echo", "hi"], config=SandboxConfig())
    assert out[0] == "sandbox-exec"
    assert out[1] == "-p"
    assert "(version 1)" in out[2]
    assert "(deny default)" in out[2]
    # Trailing argv must be preserved in order.
    assert out[-2:] == ["echo", "hi"]


def test_macos_profile_includes_write_paths_and_network_flag() -> None:
    """User-supplied write_paths + network_allowed plumb through to the profile.

    Note ``read_paths`` is currently advisory on this strategy (file-read*
    is global — see module docstring). ``write_paths`` is the actual
    knob users care about.
    """
    s = MacOSSandboxExecStrategy()
    cfg = SandboxConfig(
        write_paths=("/Users/test",),
        network_allowed=True,
    )
    out = s.explain(["echo", "x"], config=cfg)
    profile = out[2]
    assert '"/Users/test"' in profile
    assert "(allow network*)" in profile

    cfg2 = SandboxConfig(network_allowed=False)
    out2 = s.explain(["echo", "x"], config=cfg2)
    assert "(allow network*)" not in out2[2]


def test_macos_profile_rejects_quote_in_path() -> None:
    """Defensive: refuse paths containing quotes/backslashes."""
    s = MacOSSandboxExecStrategy()
    # write_paths flows directly into the profile string, so a malformed
    # path triggers the validator. read_paths is also validated even
    # though it's not currently embedded — defence in depth.
    cfg = SandboxConfig(write_paths=('/etc/"weird"/path',))
    with pytest.raises(ValueError, match="quote/backslash"):
        s.explain(["echo", "x"], config=cfg)
    cfg2 = SandboxConfig(read_paths=('/etc/"weird"/path',))
    with pytest.raises(ValueError, match="quote/backslash"):
        s.explain(["echo", "x"], config=cfg2)


@pytest.mark.skipif(
    platform.system() != "Darwin" or not MacOSSandboxExecStrategy().is_available(),
    reason="sandbox-exec not available on this host",
)
def test_macos_runs_simple_echo() -> None:
    """End-to-end echo through sandbox-exec on macOS."""
    s = MacOSSandboxExecStrategy()
    result = asyncio.run(s.run(["echo", "hi"], config=SandboxConfig()))
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.strategy_name == "macos_sandbox_exec"


# ---------------------------------------------------------------------------
# LinuxBwrapStrategy
# ---------------------------------------------------------------------------


def test_linux_strategy_is_available_only_on_linux() -> None:
    s = LinuxBwrapStrategy()
    if platform.system() == "Linux":
        from shutil import which
        expected = which("bwrap") is not None
        assert s.is_available() is expected
    else:
        assert s.is_available() is False


def test_linux_strategy_explain_returns_wrapped_argv() -> None:
    """First token is either ``prlimit`` (when available + memory cap set) or ``bwrap``."""
    s = LinuxBwrapStrategy()
    out = s.explain(["echo", "hi"], config=SandboxConfig())
    # Either prlimit prefix + bwrap, or bwrap directly. Both end with our argv.
    assert "bwrap" in out
    assert out[-2:] == ["echo", "hi"]
    # The unshare-pid flag is unconditional containment.
    assert "--unshare-pid" in out


def test_linux_strategy_unshares_net_when_network_denied() -> None:
    s = LinuxBwrapStrategy()
    out = s.explain(["echo", "hi"], config=SandboxConfig(network_allowed=False))
    assert "--unshare-net" in out
    out2 = s.explain(["echo", "hi"], config=SandboxConfig(network_allowed=True))
    assert "--unshare-net" not in out2


def test_linux_strategy_includes_user_paths() -> None:
    s = LinuxBwrapStrategy()
    out = s.explain(
        ["echo", "x"],
        config=SandboxConfig(read_paths=("/srv/data",), write_paths=("/srv/out",)),
    )
    # Path appears as both ``--ro-bind /srv/data /srv/data`` and ``--bind /srv/out /srv/out``.
    ro_idx = out.index("--ro-bind") if "--ro-bind" in out else -1
    assert ro_idx >= 0
    assert "/srv/data" in out
    assert "/srv/out" in out


@pytest.mark.skipif(
    platform.system() != "Linux" or not LinuxBwrapStrategy().is_available(),
    reason="bwrap not available on this host",
)
def test_linux_runs_simple_echo() -> None:
    s = LinuxBwrapStrategy()
    result = asyncio.run(s.run(["echo", "hi"], config=SandboxConfig()))
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert result.strategy_name == "linux_bwrap"


# ---------------------------------------------------------------------------
# DockerStrategy
# ---------------------------------------------------------------------------


def test_docker_strategy_explain_returns_wrapped_argv() -> None:
    s = DockerStrategy()
    out = s.explain(["echo", "hi"], config=SandboxConfig())
    assert out[0] == "docker"
    assert out[1] == "run"
    assert "--rm" in out
    assert "alpine:latest" in out
    assert out[-2:] == ["echo", "hi"]


def test_docker_strategy_passes_memory_and_cpu() -> None:
    s = DockerStrategy()
    out = s.explain(
        ["echo", "x"],
        config=SandboxConfig(memory_mb_limit=128, cpu_seconds_limit=60),
    )
    assert "--memory" in out
    assert "128m" in out
    assert "--cpus" in out
    # 60 // 30 = 2; clamped between 1 and 2 → 2 cores
    assert "2" in out


def test_docker_strategy_network_none_when_denied() -> None:
    s = DockerStrategy()
    out_deny = s.explain(["echo", "x"], config=SandboxConfig(network_allowed=False))
    assert "--network" in out_deny
    assert "none" in out_deny

    out_allow = s.explain(["echo", "x"], config=SandboxConfig(network_allowed=True))
    assert "--network" not in out_allow


def test_docker_strategy_image_overridable() -> None:
    s = DockerStrategy()
    out = s.explain(
        ["echo", "x"],
        config=SandboxConfig(image="python:3.12-alpine"),
    )
    assert "python:3.12-alpine" in out


def test_docker_is_available_when_binary_missing() -> None:
    """If ``docker`` is not on PATH, the strategy is unavailable.

    We instantiate WITH the host's real ``docker``; the unit test for
    "binary missing" mocks ``shutil.which`` to return None.
    """
    with patch("opencomputer.sandbox.docker.shutil.which", return_value=None):
        s = DockerStrategy()
    assert s.is_available() is False


# ---------------------------------------------------------------------------
# auto_strategy + run_sandboxed dispatch
# ---------------------------------------------------------------------------


def test_auto_picks_first_available() -> None:
    """When the host-native strategy is available, ``auto`` returns it."""
    sysname = platform.system()
    # Force every strategy to "available" so the order is deterministic.
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=True),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=True),
        patch.object(DockerStrategy, "is_available", return_value=True),
    ):
        picked = auto_strategy()
    if sysname == "Darwin":
        assert picked.name == "macos_sandbox_exec"
    elif sysname == "Linux":
        assert picked.name == "linux_bwrap"
    else:
        assert picked.name == "docker"


def test_auto_falls_back_to_docker() -> None:
    """When host-native is unavailable but docker is, auto picks docker."""
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=True),
    ):
        picked = auto_strategy()
    assert picked.name == "docker"


def test_auto_raises_if_no_strategy_available() -> None:
    """All strategies mocked unavailable; raises ``SandboxUnavailable``."""
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=False),
    ):
        with pytest.raises(SandboxUnavailable, match="no sandbox strategy"):
            auto_strategy()


def test_run_sandboxed_dispatches_by_config_strategy() -> None:
    """``config.strategy='docker'`` calls Docker (mock the actual run)."""
    fake_result = SandboxResult(
        exit_code=0,
        stdout="hi\n",
        stderr="",
        duration_seconds=0.01,
        wrapped_command=["docker", "run", "--rm", "echo", "hi"],
        strategy_name="docker",
    )

    async def _fake_run(self, argv, *, config, stdin=None, cwd=None):  # noqa: ANN001
        return fake_result

    with (
        patch.object(DockerStrategy, "is_available", return_value=True),
        patch.object(DockerStrategy, "run", _fake_run),
    ):
        result = asyncio.run(
            run_sandboxed(["echo", "hi"], config=SandboxConfig(strategy="docker"))
        )
    assert result is fake_result


def test_run_sandboxed_named_strategy_unavailable_raises() -> None:
    """Named strategy unavailable on host → SandboxUnavailable."""
    with patch.object(DockerStrategy, "is_available", return_value=False):
        with pytest.raises(SandboxUnavailable, match="docker"):
            asyncio.run(
                run_sandboxed(["echo", "hi"], config=SandboxConfig(strategy="docker"))
            )


def test_run_sandboxed_unknown_strategy_raises() -> None:
    # Use ``type: ignore`` for the deliberate bad value — runtime check.
    with pytest.raises(SandboxUnavailable, match="unknown sandbox strategy"):
        asyncio.run(
            run_sandboxed(
                ["echo", "x"],
                config=SandboxConfig(strategy="bogus"),  # type: ignore[arg-type]
            )
        )


def test_run_sandboxed_none_strategy_runs_directly() -> None:
    result = asyncio.run(
        run_sandboxed(["echo", "hi"], config=SandboxConfig(strategy="none"))
    )
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.strategy_name == "none"


def test_run_sandboxed_auto_unavailable_message_mentions_opt_out() -> None:
    """Auto-fail message names the ``strategy='none'`` opt-out."""
    with (
        patch.object(MacOSSandboxExecStrategy, "is_available", return_value=False),
        patch.object(LinuxBwrapStrategy, "is_available", return_value=False),
        patch.object(DockerStrategy, "is_available", return_value=False),
    ):
        with pytest.raises(SandboxUnavailable) as excinfo:
            asyncio.run(run_sandboxed(["echo", "x"]))
    assert "none" in str(excinfo.value)


# ---------------------------------------------------------------------------
# SandboxConfig defaults
# ---------------------------------------------------------------------------


def test_sandbox_config_default_blocks_network() -> None:
    cfg = SandboxConfig()
    assert cfg.network_allowed is False
    assert cfg.strategy == "auto"
    assert cfg.cpu_seconds_limit == 60
    assert cfg.memory_mb_limit == 512
    assert cfg.read_paths == ()
    assert cfg.write_paths == ()
    assert "PATH" in cfg.allowed_env_vars
    assert "HOME" in cfg.allowed_env_vars
    assert cfg.image == "alpine:latest"


def test_sandbox_config_is_frozen() -> None:
    cfg = SandboxConfig()
    with pytest.raises((AttributeError, Exception)):
        cfg.network_allowed = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SandboxResult shape
# ---------------------------------------------------------------------------


def test_sandbox_result_fields() -> None:
    r = SandboxResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.1,
        wrapped_command=["echo", "ok"],
        strategy_name="none",
    )
    assert r.exit_code == 0
    assert r.strategy_name == "none"
    # Frozen — assignment must fail.
    with pytest.raises((AttributeError, Exception)):
        r.exit_code = 1  # type: ignore[misc]
