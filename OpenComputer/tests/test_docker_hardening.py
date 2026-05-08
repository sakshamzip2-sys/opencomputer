"""Tests for Hermes-parity Docker security flags."""
from __future__ import annotations

from opencomputer.sandbox.docker import _SECURITY_ARGS, DockerStrategy
from plugin_sdk.sandbox import SandboxConfig


def _make_config() -> SandboxConfig:
    return SandboxConfig(
        memory_mb_limit=512,
        cpu_seconds_limit=30,
        network_allowed=False,
        read_paths=(),
        write_paths=(),
        allowed_env_vars=(),
        image="alpine:latest",
    )


def test_security_args_includes_cap_drop_all():
    assert "--cap-drop" in _SECURITY_ARGS
    idx = _SECURITY_ARGS.index("--cap-drop")
    assert _SECURITY_ARGS[idx + 1] == "ALL"


def test_security_args_includes_no_new_privileges():
    assert "--security-opt" in _SECURITY_ARGS
    idx = _SECURITY_ARGS.index("--security-opt")
    assert _SECURITY_ARGS[idx + 1] == "no-new-privileges"


def test_security_args_includes_pids_limit():
    assert "--pids-limit" in _SECURITY_ARGS
    idx = _SECURITY_ARGS.index("--pids-limit")
    assert _SECURITY_ARGS[idx + 1] == "256"


def test_security_args_includes_three_tmpfs_mounts():
    assert _SECURITY_ARGS.count("--tmpfs") == 3


def test_security_args_tmpfs_have_correct_options():
    pairs = [
        (a, b)
        for a, b in zip(_SECURITY_ARGS, _SECURITY_ARGS[1:], strict=False)
        if a == "--tmpfs"
    ]
    values = [v for _, v in pairs]
    # /tmp: rw + nosuid + 512m (no noexec — pip/npm temp files often need exec)
    assert any(
        v.startswith("/tmp:") and "nosuid" in v and "size=512m" in v
        for v in values
    )
    # /var/tmp: rw + noexec + nosuid + 256m
    assert any(
        v.startswith("/var/tmp:")
        and "noexec" in v
        and "nosuid" in v
        and "size=256m" in v
        for v in values
    )
    # /run: rw + noexec + nosuid + 64m
    assert any(
        v.startswith("/run:")
        and "noexec" in v
        and "nosuid" in v
        and "size=64m" in v
        for v in values
    )


def test_security_args_includes_three_capability_adds():
    cap_adds = [
        b
        for a, b in zip(_SECURITY_ARGS, _SECURITY_ARGS[1:], strict=False)
        if a == "--cap-add"
    ]
    assert "DAC_OVERRIDE" in cap_adds
    assert "CHOWN" in cap_adds
    assert "FOWNER" in cap_adds


def test_wrap_argv_includes_all_security_args():
    """The full ``_SECURITY_ARGS`` block appears verbatim in the wrap argv.

    Asserts on a sublist match — every entry in ``_SECURITY_ARGS``
    must be present in order somewhere in the argv produced by
    ``DockerStrategy._wrap``.
    """
    strat = DockerStrategy()
    argv = strat._wrap(
        ["/bin/sh", "-c", "echo ok"],
        config=_make_config(),
        container_name="test-container",
    )

    # Find _SECURITY_ARGS as a contiguous slice within argv.
    n = len(_SECURITY_ARGS)
    for i in range(len(argv) - n + 1):
        if argv[i : i + n] == _SECURITY_ARGS:
            return  # Found a verbatim contiguous slice — OK.
    raise AssertionError(
        f"Security args block not found verbatim in argv: {argv}"
    )


def test_wrap_argv_security_block_after_cpus():
    """Security block must come AFTER the --cpus flag and BEFORE image.

    Tests the integration ordering; without this the network/path/env
    flags could come between cpus and security and the audit story
    becomes harder to read.
    """
    strat = DockerStrategy()
    argv = strat._wrap(
        ["/bin/sh", "-c", "echo ok"],
        config=_make_config(),
        container_name="test-container",
    )
    cpus_idx = argv.index("--cpus")
    cap_drop_idx = argv.index("--cap-drop")
    image_idx = argv.index("alpine:latest")
    assert cpus_idx < cap_drop_idx
    assert cap_drop_idx < image_idx
