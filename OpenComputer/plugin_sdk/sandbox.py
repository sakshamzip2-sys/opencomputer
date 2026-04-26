"""Public sandbox primitives — pluggable containment for tool execution.

These types are re-exported via :mod:`plugin_sdk.__init__`. Concrete
strategies live in ``opencomputer/sandbox/`` (internal — may evolve);
plugins and tools should depend only on the ABC + dataclasses defined
here.

Typical usage (the runner lives in ``opencomputer.sandbox.runner`` —
plugins import the public types from here, callers in the core import
the runner from there)::

    from plugin_sdk import SandboxConfig
    # In core / tools (not plugins): the helper is at
    # ``opencomputer.sandbox.run_sandboxed``.

    cfg = SandboxConfig(strategy="auto", network_allowed=False)
    result = await run_sandboxed(["echo", "hi"], config=cfg)
    print(result.stdout)  # "hi\n"

Phase 3.E (master plan §3.E) ships this primitive only; tool wiring
(Bash, OI bridge, etc.) lands in later phases.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import ClassVar, Literal


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Outcome of a single sandboxed invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    wrapped_command: list[str]
    """The fully wrapped argv that was actually executed (for debugging /
    auditing). For the ``"none"`` strategy this equals the original argv."""
    strategy_name: str
    """Short id of the strategy that produced this result."""


# Allowed values for ``SandboxConfig.strategy``. ``"auto"`` lets
# ``opencomputer.sandbox.auto_strategy`` pick the best available for the host.
SandboxStrategyName = Literal[
    "auto",
    "macos_sandbox_exec",
    "linux_bwrap",
    "docker",
    "ssh",
    "none",
]


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Per-invocation sandbox policy.

    All fields are immutable; pass a fresh ``SandboxConfig`` per call when
    the policy varies. Defaults are conservative: deny network, no extra
    paths, 60s wall-clock cap, 512 MB memory cap.
    """

    strategy: SandboxStrategyName = "auto"
    cpu_seconds_limit: int = 60
    """Wall-clock cap, enforced via subprocess timeout."""
    memory_mb_limit: int = 512
    """Best-effort memory cap. Passed to bwrap (via prlimit) and Docker
    (``--memory``); ignored by macOS ``sandbox-exec`` which has no native
    rlimit support — document loudly so callers don't assume otherwise."""
    network_allowed: bool = False
    """When False, the sandbox blocks outbound network. Default deny."""
    read_paths: tuple[str, ...] = ()
    """Extra paths the sandboxed process may read. Each strategy ships a
    minimal base profile (``/usr/lib``, ``/usr/bin``, ``/bin``,
    ``/etc/resolv.conf`` etc.); these augment that baseline."""
    write_paths: tuple[str, ...] = ()
    """Extra paths the sandboxed process may write. A per-invocation tmp
    dir is auto-injected and need not be listed here."""
    allowed_env_vars: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL")
    """Env-var allowlist; everything else is stripped before exec."""
    image: str = "alpine:latest"
    """Image used by the Docker strategy. Ignored by other strategies."""

    ssh_host: str | None = None
    """Target for the ``"ssh"`` strategy in ``user@host`` form (host alone
    is also accepted). Ignored by other strategies. Validated against a
    strict regex before use; metacharacters refused outright."""

    # Reserved for future expansion (e.g. seccomp profile, syscall allowlist).
    # Kept frozen so callers can hash + compare configs.
    _reserved: tuple[str, ...] = field(default=(), repr=False)


class SandboxUnavailable(RuntimeError):  # noqa: N818 — public name is load-bearing; no ``Error`` suffix per spec
    """Raised when a requested strategy can't run on this host.

    The ``"auto"`` strategy raises this only when **no** strategy is
    available — the helper text suggests installing ``bwrap`` / Docker
    or opting out via ``SandboxConfig(strategy="none")``.
    """


class SandboxStrategy(abc.ABC):
    """Abstract base class for sandbox strategies.

    Subclasses live in ``opencomputer/sandbox/`` and are picked by
    :func:`opencomputer.sandbox.auto_strategy` based on the host platform
    and available binaries.

    Implementations MUST:

    1.  Spawn the wrapped command with :func:`asyncio.create_subprocess_exec`
        (never the blocking :mod:`subprocess` module) so the event loop is
        not blocked.
    2.  Strip env vars not listed in ``config.allowed_env_vars`` before
        passing them through.
    3.  Enforce ``config.cpu_seconds_limit`` via timeout — kill the process
        and return a non-zero exit + sentinel stderr on overrun.
    4.  Set ``SandboxResult.strategy_name`` to ``self.name``.
    """

    name: ClassVar[str]
    """Short id (``"macos_sandbox_exec"`` / ``"linux_bwrap"`` / ``"docker"`` /
    ``"none"``). Used by :func:`opencomputer.sandbox.runner.run_sandboxed`
    to dispatch named-strategy requests."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Quick capability check. Cheap, side-effect-free, cached.

        Returns ``True`` only if the strategy can run on the current host
        — typically a platform check + a ``shutil.which()`` for the wrapper
        binary. Heavy probes (``docker info``) should still be cached so
        the call is effectively constant-time after the first invocation.
        """

    @abc.abstractmethod
    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute ``argv`` inside the sandbox; return the captured result.

        Raises :class:`SandboxUnavailable` if the strategy isn't available
        on this host (callers should normally check :meth:`is_available`
        first; the runner does).
        """

    @abc.abstractmethod
    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        """Return the wrapped command without running it.

        Useful for ``--dry-run`` style introspection and for surfacing
        the actual containment invocation in audit logs.
        """


__all__ = [
    "SandboxConfig",
    "SandboxResult",
    "SandboxStrategy",
    "SandboxStrategyName",
    "SandboxUnavailable",
]
