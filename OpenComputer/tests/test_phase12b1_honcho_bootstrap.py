"""Phase 12b1 / Sub-project A — Task A3: ``bootstrap.ensure_started()``.

Safe, idempotent bring-up of the Honcho Docker stack:

  1. Pre-flight: docker detection.
  2. Port-collision detection (sockets, not subprocess).
  3. Pull image if missing.
  4. ``docker compose up -d``.
  5. Health poll every 2s until healthy or ``timeout_s`` elapsed.
  6. Success.

Idempotent — if the stack is already running + healthy, returns
``(True, "already running...")`` without pulling or starting.

All tests mock subprocess / socket so Docker is never actually invoked.
"""

from __future__ import annotations

import errno
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"


def _load_honcho_bootstrap_module():
    """Load ``extensions/memory-honcho/bootstrap.py`` under a synthetic name.

    The extension dir has a hyphen so it's not an importable package — use
    ``importlib.util`` like the plugin loader does. Must register in
    ``sys.modules`` BEFORE exec so ``@dataclass(slots=True)`` can look up
    the owning module via ``cls.__module__``.
    """
    mod_name = "_honcho_a3_bootstrap_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, _EXT_DIR / "bootstrap.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ok_cp(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Helper: successful ``CompletedProcess`` with configurable output."""
    return subprocess.CompletedProcess(
        args=["docker"], returncode=0, stdout=stdout, stderr=stderr
    )


def _fail_cp(stderr: bytes | str = b"") -> subprocess.CompletedProcess:
    """Helper: failed ``CompletedProcess`` with stderr."""
    return subprocess.CompletedProcess(
        args=["docker"], returncode=1, stdout="", stderr=stderr
    )


# ─── Test #1: Docker missing ────────────────────────────────────────────


def test_ensure_started_returns_false_if_docker_absent() -> None:
    """Pre-flight catches missing Docker and returns install guidance."""
    bootstrap = _load_honcho_bootstrap_module()

    with patch.object(bootstrap, "detect_docker", return_value=(False, False)):
        ok, msg = bootstrap.ensure_started()

    assert ok is False
    assert "Docker" in msg


# ─── Test #2: Compose v2 plugin missing ─────────────────────────────────


def test_ensure_started_returns_false_if_compose_v2_absent() -> None:
    """Pre-flight catches compose-v2 missing — install docker-compose-plugin."""
    bootstrap = _load_honcho_bootstrap_module()

    with patch.object(bootstrap, "detect_docker", return_value=(True, False)):
        ok, msg = bootstrap.ensure_started()

    assert ok is False
    assert "v2 plugin" in msg


# ─── Test #3: Port collision ────────────────────────────────────────────


def test_ensure_started_detects_port_collision_before_pulling() -> None:
    """A port already bound by a non-Honcho process must abort BEFORE pull."""
    bootstrap = _load_honcho_bootstrap_module()

    # _is_stack_healthy returns False (stack not up) so we proceed past the
    # idempotency short-circuit. Then port-check fails on the first port.
    with (
        patch.object(bootstrap, "detect_docker", return_value=(True, True)),
        patch.object(bootstrap, "_is_stack_healthy", return_value=False),
        patch.object(
            bootstrap,
            "_check_port_available",
            side_effect=lambda port: False,  # every port reports "already bound"
        ),
        patch.object(bootstrap, "_compose") as fake_compose,
    ):
        ok, msg = bootstrap.ensure_started()

    assert ok is False
    assert "port" in msg.lower(), f"expected 'port' in error message: {msg!r}"
    # Critical: pull was NOT called because we bailed on port collision first.
    pull_calls = [c for c in fake_compose.call_args_list if "pull" in c.args]
    up_calls = [c for c in fake_compose.call_args_list if "up" in c.args]
    assert pull_calls == [], f"pull should not run when port is blocked: {pull_calls}"
    assert up_calls == [], f"up should not run when port is blocked: {up_calls}"


# ─── Test #4: Happy path — pull then up then healthy ────────────────────


def test_ensure_started_pulls_and_starts_when_clean() -> None:
    """Clean machine: detect-ok, ports-free, pull-ok, up-ok, healthy-quickly."""
    bootstrap = _load_honcho_bootstrap_module()

    # _is_stack_healthy is called twice in the happy path:
    #   1. Before pull (idempotency check) — must return False so we proceed.
    #   2. In the post-up health poll — return True to succeed fast.
    healthy_responses = iter([False, True])

    def _compose_side_effect(*args, **_kwargs):
        return _ok_cp()

    with (
        patch.object(bootstrap, "detect_docker", return_value=(True, True)),
        patch.object(
            bootstrap,
            "_is_stack_healthy",
            side_effect=lambda: next(healthy_responses),
        ),
        patch.object(bootstrap, "_check_port_available", return_value=True),
        patch.object(
            bootstrap, "_compose", side_effect=_compose_side_effect
        ) as fake_compose,
        patch.object(bootstrap.time, "sleep", return_value=None),
    ):
        ok, msg = bootstrap.ensure_started(timeout_s=10)

    assert ok is True, f"expected success, got ({ok!r}, {msg!r})"
    # Must have called pull then up in that order.
    all_positional = [c.args for c in fake_compose.call_args_list]
    # Find pull and up calls (we don't assume no other calls):
    pull_indexes = [i for i, a in enumerate(all_positional) if "pull" in a]
    up_indexes = [i for i, a in enumerate(all_positional) if "up" in a]
    assert pull_indexes, f"pull was never called: {all_positional}"
    assert up_indexes, f"up was never called: {all_positional}"
    assert pull_indexes[0] < up_indexes[0], (
        f"pull must run before up; got pull@{pull_indexes} up@{up_indexes}"
    )


# ─── Test #5: Pull fails — fast-fail with stderr snippet ────────────────


def test_ensure_started_fails_fast_when_pull_returns_nonzero() -> None:
    """A non-zero ``docker compose pull`` → bail before ``up`` is attempted."""
    bootstrap = _load_honcho_bootstrap_module()

    def _compose_side_effect(*args, **_kwargs):
        if "pull" in args:
            return _fail_cp(stderr=b"403 Forbidden: image access denied")
        # Any other compose call (shouldn't happen if pull fails first) →
        # succeed quietly so a stray call doesn't mask the pull failure.
        return _ok_cp()

    with (
        patch.object(bootstrap, "detect_docker", return_value=(True, True)),
        patch.object(bootstrap, "_is_stack_healthy", return_value=False),
        patch.object(bootstrap, "_check_port_available", return_value=True),
        patch.object(
            bootstrap, "_compose", side_effect=_compose_side_effect
        ) as fake_compose,
    ):
        ok, msg = bootstrap.ensure_started()

    assert ok is False
    assert "pull failed" in msg.lower(), f"expected 'pull failed' in {msg!r}"
    assert "403" in msg, f"expected stderr snippet in error: {msg!r}"
    # Up must not have been called.
    up_calls = [c for c in fake_compose.call_args_list if "up" in c.args]
    assert up_calls == [], f"up should not run after pull failure: {up_calls}"


# ─── Test #6: Health poll times out ─────────────────────────────────────


def test_ensure_started_times_out_when_stack_never_becomes_healthy() -> None:
    """If health never flips to true within timeout_s, return failure."""
    bootstrap = _load_honcho_bootstrap_module()

    with (
        patch.object(bootstrap, "detect_docker", return_value=(True, True)),
        # Always False — never healthy.
        patch.object(bootstrap, "_is_stack_healthy", return_value=False),
        patch.object(bootstrap, "_check_port_available", return_value=True),
        patch.object(bootstrap, "_compose", return_value=_ok_cp()),
        # Monkeypatch time.sleep so the test doesn't actually wait seconds.
        patch.object(bootstrap.time, "sleep", return_value=None),
    ):
        ok, msg = bootstrap.ensure_started(timeout_s=1)

    assert ok is False
    assert "did not become healthy" in msg.lower(), f"unexpected msg: {msg!r}"


# ─── Test #7: Idempotent — already healthy ──────────────────────────────


def test_ensure_started_is_idempotent_when_already_healthy() -> None:
    """Stack already healthy → return (True, "already running..."); no pull, no up."""
    bootstrap = _load_honcho_bootstrap_module()

    with (
        patch.object(bootstrap, "detect_docker", return_value=(True, True)),
        # Stack is healthy → the idempotency branch short-circuits everything.
        patch.object(bootstrap, "_is_stack_healthy", return_value=True),
        patch.object(bootstrap, "_compose") as fake_compose,
        patch.object(bootstrap, "_check_port_available") as fake_port,
    ):
        ok, msg = bootstrap.ensure_started()

    assert ok is True
    assert "already running" in msg.lower(), f"unexpected msg: {msg!r}"
    # Neither pull nor up nor port-check should have run.
    pull_calls = [c for c in fake_compose.call_args_list if "pull" in c.args]
    up_calls = [c for c in fake_compose.call_args_list if "up" in c.args]
    assert pull_calls == [], f"pull called during idempotent early-return: {pull_calls}"
    assert up_calls == [], f"up called during idempotent early-return: {up_calls}"
    # Port check is also redundant — if already healthy, ports are ours.
    assert fake_port.call_count == 0, "port check redundant when already healthy"


# ─── Meta: _check_port_available actually uses socket bind ──────────────


def test_check_port_available_uses_socket_bind() -> None:
    """Sanity: the port-availability helper uses socket.bind with EADDRINUSE."""
    bootstrap = _load_honcho_bootstrap_module()

    class _FakeSocket:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def setsockopt(self, *_):
            pass

        def bind(self, _addr):
            raise OSError(errno.EADDRINUSE, "address in use")

    with patch("socket.socket", _FakeSocket):
        assert bootstrap._check_port_available(8000) is False

    class _CleanSocket(_FakeSocket):
        def bind(self, _addr):
            return None

    with patch("socket.socket", _CleanSocket):
        assert bootstrap._check_port_available(8000) is True
