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

from opencomputer.sandbox.auto import auto_strategy
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
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
        s = MacOSSandboxExecStrategy()
    elif name == "linux_bwrap":
        s = LinuxBwrapStrategy()
    elif name == "docker":
        s = DockerStrategy()
    else:
        raise SandboxUnavailable(
            f"unknown sandbox strategy {name!r}; "
            "valid: auto / macos_sandbox_exec / linux_bwrap / docker / none"
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
) -> SandboxResult:
    """Run ``argv`` inside the configured sandbox; return a SandboxResult.

    ``config=None`` uses :class:`~plugin_sdk.SandboxConfig` defaults
    (auto strategy, 60 s wall-clock cap, 512 MB RAM cap, network denied,
    PATH/HOME/LANG/LC_ALL env passthrough).
    """
    cfg = config or SandboxConfig()

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
