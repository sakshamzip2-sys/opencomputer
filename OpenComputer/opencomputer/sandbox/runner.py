"""``run_sandboxed`` — one-call helper for tools that need containment.

Resolves the strategy implied by ``config.strategy`` (or the host
default when ``"auto"``) and runs ``argv`` inside it. Catches
:class:`~plugin_sdk.SandboxUnavailable` and re-raises with a clearer
message that names the ``strategy="none"`` opt-out.

Example::

    from plugin_sdk import SandboxConfig
    from opencomputer.sandbox import run_sandboxed

    result = await run_sandboxed(
        ["python", "-c", "print('hi')"],
        config=SandboxConfig(network_allowed=False),
    )
    assert result.exit_code == 0
"""

from __future__ import annotations

from dataclasses import replace

from opencomputer.sandbox.auto import auto_strategy
from opencomputer.sandbox.daytona import DaytonaSandboxStrategy
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.e2b import E2BSandboxStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.modal import ModalSandboxStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
from opencomputer.sandbox.policy import (
    SandboxPolicy,
    SandboxScope,
    SandboxScopeContext,
    scope_key,
)
from opencomputer.sandbox.ssh import SSHSandboxStrategy
from plugin_sdk.sandbox import (
    SandboxConfig,
    SandboxResult,
    SandboxStrategy,
    SandboxUnavailable,
)


def _named_strategy(name: str) -> SandboxStrategy:
    """Resolve a named strategy. Raises ``SandboxUnavailable`` if absent."""
    if name == "none":
        return NoneSandboxStrategy()
    if name == "macos_sandbox_exec":
        s: SandboxStrategy = MacOSSandboxExecStrategy()
    elif name == "linux_bwrap":
        s = LinuxBwrapStrategy()
    elif name == "docker":
        s = DockerStrategy()
    elif name == "ssh":
        s = SSHSandboxStrategy()
    elif name == "e2b":
        s = E2BSandboxStrategy()
    elif name == "daytona":
        s = DaytonaSandboxStrategy()
    elif name == "modal":
        s = ModalSandboxStrategy()
    else:
        raise SandboxUnavailable(
            f"unknown sandbox strategy {name!r}; "
            "valid: auto / macos_sandbox_exec / linux_bwrap / docker / ssh / "
            "e2b / daytona / modal / none"
        )
    if not s.is_available():
        raise SandboxUnavailable(
            f"sandbox strategy {name!r} not available on this host; "
            "use SandboxConfig(strategy='auto') to autodetect, or "
            "SandboxConfig(strategy='none') to opt out of containment"
        )
    return s


async def run_sandboxed(
    argv: list[str],
    *,
    config: SandboxConfig | None = None,
    stdin: bytes | None = None,
    cwd: str | None = None,
    policy: SandboxPolicy | None = None,
    scope_ctx: SandboxScopeContext | None = None,
) -> SandboxResult:
    """Run ``argv`` inside the configured sandbox; return a SandboxResult.

    ``config=None`` uses :class:`~plugin_sdk.SandboxConfig` defaults
    (auto strategy, 60 s wall-clock cap, 512 MB RAM cap, network denied,
    PATH/HOME/LANG/LC_ALL env passthrough).

    ``policy`` (the active :class:`~opencomputer.sandbox.policy.SandboxPolicy`)
    selects how the container is scoped: when supplied, the container key is
    derived via :func:`~opencomputer.sandbox.policy.scope_key` and threaded
    through ``config.container_key``. ``scope_ctx`` carries the session /
    agent ids that ``session`` / ``agent`` scope key on. With no ``policy``
    — the default — behavior is unchanged (a fresh container per call). An
    explicit ``config.container_key`` is never overwritten.
    """
    cfg = config or SandboxConfig()

    if policy is not None and cfg.container_key is None:
        # Only a *poolable* scope's key drives container reuse. ``tool``
        # produces a fresh per-call uuid and ``none`` an empty key —
        # threading either as ``container_key`` would make the Docker
        # strategy pool a never-reused container per call. ``tool`` /
        # ``none`` keep the transient path (``container_key`` stays None).
        if policy.scope in (
            SandboxScope.SESSION,
            SandboxScope.AGENT,
            SandboxScope.SHARED,
        ):
            cfg = replace(cfg, container_key=scope_key(policy, scope_ctx))

    if cfg.strategy == "auto":
        try:
            strategy = auto_strategy(cfg)
        except SandboxUnavailable as e:
            # Re-raise with explicit guidance — the original message
            # already mentions opt-out, but we double down so callers
            # who only catch the runner exception get the same advice.
            raise SandboxUnavailable(
                f"{e} (caller can pass SandboxConfig(strategy='none') to skip containment)"
            ) from e
    else:
        strategy = _named_strategy(cfg.strategy)

    return await strategy.run(argv, config=cfg, stdin=stdin, cwd=cwd)
