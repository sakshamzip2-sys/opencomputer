"""``auto_strategy`` — pick the best available sandbox for the host.

Selection order::

    Darwin   → MacOSSandboxExecStrategy
    Linux    → LinuxBwrapStrategy
    Other    → DockerStrategy (also tried on Darwin/Linux as a fallback)

Raises :class:`~plugin_sdk.SandboxUnavailable` if NO strategy is
available, with a helpful message pointing at install options + the
``strategy="none"`` opt-out.
"""

from __future__ import annotations

import platform

from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from plugin_sdk.sandbox import SandboxConfig, SandboxStrategy, SandboxUnavailable


def auto_strategy(config: SandboxConfig | None = None) -> SandboxStrategy:
    """Return the highest-preference available strategy.

    The optional ``config`` argument is reserved for future use (e.g. to
    let callers blacklist a specific strategy or pre-pull a Docker image
    before selection); today it's accepted but not consulted. Pass it
    anyway so the API is forward-compatible.
    """
    del config  # reserved; see docstring

    sysname = platform.system()
    candidates: list[SandboxStrategy] = []
    if sysname == "Darwin":
        candidates.append(MacOSSandboxExecStrategy())
    elif sysname == "Linux":
        candidates.append(LinuxBwrapStrategy())
    # Docker is the universal fallback — try it last on every host.
    candidates.append(DockerStrategy())

    for s in candidates:
        if s.is_available():
            return s

    raise SandboxUnavailable(
        "no sandbox strategy available on this host; "
        "install bwrap (Linux), Docker (any), or pass "
        "SandboxConfig(strategy='none') to opt out of containment"
    )
