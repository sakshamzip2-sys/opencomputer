"""Daytona strategy — containment via an ephemeral Daytona cloud sandbox.

Each call: opens an ``async with AsyncDaytona()`` client context, creates a
fresh sandbox via ``client.create()``, runs the wrapped command via
``sandbox.process.exec()``, and deletes the sandbox in ``finally``. Client
cleanup is automatic via the async-with exit — the SDK exposes
``__aenter__``/``__aexit__`` + ``async close()`` (verified from
``daytona/_async/daytona.py``).

Availability: the optional ``daytona`` package must import AND
``DAYTONA_API_KEY`` must be set (``pip install opencomputer[daytona]``; key
from https://app.daytona.io/dashboard). ``is_available()`` is cheap, cached,
and never raises — mirrors :mod:`opencomputer.sandbox.e2b`.

Spike-resolved behaviours (M-1…M-4, named for parallel with ``e2b.py``):

* **M-1 — argv vs command string.** Daytona's ``process.exec`` takes a
  single shell-command string; we ``shlex.join`` argv (same pattern as
  e2b / ssh).
* **M-2 — stderr is dropped by ``ExecuteResponse``.** Verified from
  ``daytona/_async/process.py`` line 102: *"result: Standard output from
  the command"* — the SDK only captures stdout. To preserve stderr CONTENT
  (visible to ``Bash`` callers), we wrap as ``(<cmd>) 2>&1`` so stderr is
  merged into stdout. Result: every Daytona run has empty
  ``SandboxResult.stderr`` and the combined output in ``stdout``.
* **M-3 — non-zero exit does NOT raise.** Unlike e2b's
  ``CommandExitException``, Daytona returns ``ExecuteResponse(exit_code,
  result)`` and the caller reads ``exit_code``. No try/except needed for
  normal command failures.
* **M-4 — ``network_allowed=False`` is not honored in M2.** Daytona's
  ``CreateSandboxFromSnapshotParams`` supports ``network_block_all``;
  enforcing it is a noted follow-up. M2 warns and proceeds (e2b parity).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shlex
import time

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    decode_stream,
    filtered_env,
)
from plugin_sdk.sandbox import (
    SandboxConfig,
    SandboxResult,
    SandboxStrategy,
    SandboxUnavailable,
)

_log = logging.getLogger("opencomputer.sandbox.daytona")

#: Auth env var. ``DaytonaConfig`` reads it when ``api_key=None``.
_DAYTONA_API_KEY_ENV = "DAYTONA_API_KEY"


def _daytona_available() -> bool:
    """True iff the ``daytona`` package imports AND ``DAYTONA_API_KEY`` is set.

    Cheap and side-effect-free: an env-var read + an ``importlib`` spec
    lookup (no import of the SDK body). Never raises — any failure → False.
    """
    if not os.environ.get(_DAYTONA_API_KEY_ENV):
        return False
    try:
        return importlib.util.find_spec("daytona") is not None
    except (ImportError, ValueError):
        return False


class DaytonaSandboxStrategy(SandboxStrategy):
    """Wraps argv in an ephemeral Daytona cloud sandbox.

    Trust model: the wrapped command runs on Daytona's infrastructure,
    fully isolated from the local host. The sandbox is created AND deleted
    inside this one ``run`` call — the SDK's httpx client must not survive
    across event loops (the ``e2b.py`` cross-event-loop pattern).
    """

    name = "daytona"

    def __init__(self) -> None:
        # Capability probe cached at construction (parallels e2b.py).
        self._available = _daytona_available()

    def is_available(self) -> bool:
        return self._available

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        del config  # Daytona's template is fixed; ``image`` does not apply.
        # Synthetic audit marker (the real call is a network request).
        return ["daytona", "sandbox", "exec", "--", shlex.join(argv)]

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        # Lazy import: a missing optional dep must never crash module load.
        try:
            from daytona import AsyncDaytona
        except ImportError as exc:
            raise SandboxUnavailable(
                "daytona strategy: the 'daytona' package is not installed; "
                "install with `pip install opencomputer[daytona]`"
            ) from exc

        if not os.environ.get(_DAYTONA_API_KEY_ENV):
            raise SandboxUnavailable(
                "daytona strategy: DAYTONA_API_KEY is not set; obtain a key "
                "from https://app.daytona.io/dashboard and export "
                "DAYTONA_API_KEY"
            )

        # M-4: per-call network-deny isn't enforced in M2 (follow-up).
        if not config.network_allowed:
            _log.warning(
                "daytona strategy: network containment was requested "
                "(network_allowed=False) but is not enforced in M2 — the "
                "wrapped command WILL have outbound network access. Use a "
                "local strategy (docker / bwrap / sandbox-exec) to enforce "
                "network-deny today."
            )

        # Daytona's process.exec has no stdin channel.
        if stdin is not None:
            _log.warning(
                "daytona strategy: stdin was supplied (%d bytes) but "
                "Daytona's exec API has no stdin channel — the input will "
                "NOT reach the wrapped command. Use a local strategy if the "
                "command needs stdin.",
                len(stdin),
            )

        # M-1: process.exec takes a string. M-2: wrap with ``2>&1`` so the
        # original command's stderr is preserved in ``result`` (which the
        # SDK populates from stdout only).
        command = f"({shlex.join(argv)}) 2>&1"
        envs = filtered_env(config)
        wrapped = self.explain(argv, config=config)
        cap = config.cpu_seconds_limit
        start = time.monotonic()

        async def _create_and_exec() -> object:
            # async-with handles client teardown (close) even on cancel.
            async with AsyncDaytona() as client:
                sandbox = await client.create(timeout=cap)
                try:
                    return await sandbox.process.exec(
                        command, cwd=cwd, env=envs, timeout=cap,
                    )
                finally:
                    # Best-effort sandbox delete. A failed delete must not
                    # override the command's result or exception.
                    try:
                        await client.delete(sandbox, timeout=10)
                    except Exception as exc:  # noqa: BLE001 — teardown best-effort
                        _log.warning(
                            "daytona strategy: failed to delete sandbox "
                            "after run: %s",
                            exc,
                        )

        try:
            response = await asyncio.wait_for(_create_and_exec(), timeout=cap)
        except TimeoutError:
            # The whole create+exec overran ``cpu_seconds_limit``. The inner
            # ``finally`` ran the delete already; nothing more to clean.
            return SandboxResult(
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=TIMEOUT_STDERR,
                duration_seconds=time.monotonic() - start,
                wrapped_command=wrapped,
                strategy_name=self.name,
            )

        # M-3: non-zero exit is returned in ``exit_code``, never raised.
        # M-2: ``result`` is stdout-only (plus the 2>&1-merged stderr we wrap
        # the command with); ``SandboxResult.stderr`` is therefore always "".
        return SandboxResult(
            exit_code=_coerce_exit_code(getattr(response, "exit_code", 0)),
            stdout=decode_stream(getattr(response, "result", "")),
            stderr="",
            duration_seconds=time.monotonic() - start,
            wrapped_command=wrapped,
            strategy_name=self.name,
        )


def _coerce_exit_code(value: object) -> int:
    """Best-effort coerce a Daytona exit code to ``int``.

    The SDK returns an ``int``; this guards a defensive ``None`` (e.g. a
    partial response) by mapping it to ``-1``, matching the host-process
    strategies' shape.
    """
    if isinstance(value, int):
        return value
    if value is None:
        return -1
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return -1
