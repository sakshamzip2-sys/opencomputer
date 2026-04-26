"""Pluggable sandbox strategies (Phase 3.E).

This subpackage ships the concrete :class:`~plugin_sdk.SandboxStrategy`
implementations + a one-call helper :func:`run_sandboxed`. The public
contract — :class:`~plugin_sdk.SandboxConfig`,
:class:`~plugin_sdk.SandboxResult`,
:class:`~plugin_sdk.SandboxStrategy`,
:class:`~plugin_sdk.SandboxUnavailable` — lives in
:mod:`plugin_sdk.sandbox`. Plugins import the contract from there;
core / tools (BashTool, the OI bridge) import the helper from here.

Strategies
----------

* :class:`MacOSSandboxExecStrategy` — wraps argv in
  ``sandbox-exec -p <profile> ...`` on macOS. No native memory cap.
* :class:`LinuxBwrapStrategy` — wraps argv in ``bwrap --ro-bind ...``
  on Linux. Memory cap via ``prlimit`` when available.
* :class:`DockerStrategy` — cross-platform; runs argv in a transient
  container (``docker run --rm ...``).
* :class:`SSHSandboxStrategy` — runs argv on a remote host via ``ssh``.
  *Not* a containment sandbox; the remote host is trusted by the user
  who configured ``ssh_host``. Phase 1.2 of catch-up plan.
* :class:`NoneSandboxStrategy` — no containment. For trusted internal
  callers and tests. Logs a WARNING on every invocation.

Selection
---------

:func:`auto_strategy` picks the first available strategy:
``macos_sandbox_exec`` on Darwin → ``linux_bwrap`` on Linux → ``docker``
anywhere with the daemon running, else raises
:class:`~plugin_sdk.SandboxUnavailable`.

Opt-out
-------

Set ``SandboxConfig(strategy="none")`` for trusted internal use where
containment is intentionally disabled (tests, CI, single-tenant servers).
"""

from __future__ import annotations

from opencomputer.sandbox.auto import auto_strategy
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
from opencomputer.sandbox.runner import run_sandboxed
from opencomputer.sandbox.ssh import SSHSandboxStrategy

__all__ = [
    "DockerStrategy",
    "LinuxBwrapStrategy",
    "MacOSSandboxExecStrategy",
    "NoneSandboxStrategy",
    "SSHSandboxStrategy",
    "auto_strategy",
    "run_sandboxed",
]
