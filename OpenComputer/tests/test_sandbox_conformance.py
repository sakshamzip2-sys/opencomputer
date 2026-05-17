"""SandboxBackend conformance suite — Milestone 1 (sandbox-provider-breadth).

Every sandbox backend must satisfy one shared contract. ``assert_conforms``
(in ``tests/sandbox_conformance.py``) *is* that contract; this file runs it
over backends.

Coverage tiers:

* ``fake`` + ``none`` — always available, so the contract (and the
  ``assert_conforms`` harness itself) is exercised on EVERY CI run,
  regardless of platform, Docker daemon, or cloud credentials.
* ``macos_sandbox_exec`` / ``linux_bwrap`` / ``docker`` — host containment
  backends, run opportunistically (``skipif`` when the host can't run them).

Deliberately excluded: ``ssh`` needs a live ``ssh_host`` and is explicitly
not a containment sandbox; cloud backends (``e2b``, and the M2 ``daytona`` /
``modal``) are conformance-checked against MOCKED SDKs in their own test
files — running probes against a live cloud sandbox would cost money on
every test run.
"""

from __future__ import annotations

import pytest

from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
from tests.sandbox_conformance import (
    FakeSandboxBackend,
    assert_conforms,
    docker_probe_ready,
)


def test_fake_backend_conforms() -> None:
    """The in-memory reference backend satisfies the contract.

    Always runs — even on a host with no Docker, no bwrap, no cloud keys —
    so a regression in the ``assert_conforms`` harness itself is caught.
    """
    assert_conforms(FakeSandboxBackend())


def test_none_backend_conforms() -> None:
    """``NoneSandboxStrategy`` runs argv directly on the host."""
    assert_conforms(NoneSandboxStrategy())


@pytest.mark.skipif(
    not MacOSSandboxExecStrategy().is_available(),
    reason="sandbox-exec unavailable on this host",
)
def test_macos_backend_conforms() -> None:
    assert_conforms(MacOSSandboxExecStrategy())


@pytest.mark.skipif(
    not LinuxBwrapStrategy().is_available(),
    reason="bwrap unavailable on this host",
)
def test_linux_bwrap_backend_conforms() -> None:
    assert_conforms(LinuxBwrapStrategy())


@pytest.mark.skipif(
    not docker_probe_ready(),
    reason="docker daemon or alpine:latest image unavailable",
)
def test_docker_backend_conforms() -> None:
    assert_conforms(DockerStrategy())
