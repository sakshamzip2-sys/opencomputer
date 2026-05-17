"""Modal strategy — containment via an ephemeral Modal cloud sandbox.

Each call: creates a fresh ``modal.Sandbox`` via ``Sandbox.create.aio(*argv)``
— Modal's sandbox model runs the command as the entrypoint, then exposes
``returncode`` + ``stdout`` / ``stderr`` StreamReaders. The backend awaits
``sandbox.wait.aio()`` (returncode), reads both streams, and calls
``sandbox.terminate.aio()`` in ``finally``.

Availability: the optional ``modal`` package must import AND credentials
must be present — either ``MODAL_TOKEN_ID`` in the env, or
``~/.modal.toml`` (the ``modal token set`` config file). ``is_available()``
is cheap, cached, and never raises.

Spike-resolved behaviours (M-1…M-3, named for parallel with ``e2b.py`` /
``daytona.py``):

* **M-1 — argv is varargs, not a string.** Unlike e2b / daytona,
  ``Sandbox.create(*args: str)`` takes argv positionally; no
  ``shlex.join`` needed.
* **M-2 — stderr is captured separately.** Modal's Sandbox has distinct
  ``stdout`` and ``stderr`` StreamReaders, so no ``2>&1`` wrap is
  required (unlike daytona). ``SandboxResult.stderr`` carries the real
  stderr.
* **M-3 — non-zero exit does NOT raise.** ``sandbox.wait.aio()`` returns
  the returncode; the backend reads ``sandbox.returncode``. No
  exception-handling for normal command failures.
* **``app`` is optional** (verified from ``modal.Sandbox.create``
  signature). M2 passes ``app=None`` for the simplest path; the SDK uses
  an implicit/anonymous app.
* **``block_network`` is supported on create.** M2 follows the approved
  warn-and-proceed (e2b / daytona parity); enforcing
  ``network_allowed=False`` via ``block_network=True`` is a noted
  follow-up.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
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

_log = logging.getLogger("opencomputer.sandbox.modal")

#: Auth env var Modal reads first. The other path is a ``~/.modal.toml``
#: written by ``modal token set``.
_MODAL_TOKEN_ENV = "MODAL_TOKEN_ID"


def _modal_toml_exists() -> bool:
    """True iff ``~/.modal.toml`` exists. Indirection lets tests monkeypatch it."""
    return os.path.exists(os.path.expanduser("~/.modal.toml"))


def _modal_available() -> bool:
    """``modal`` package importable AND credentials present (env OR toml).

    Cheap and side-effect-free: an ``importlib`` spec lookup + an
    ``os.environ`` read + an ``os.path.exists`` fallback. Never raises.
    """
    try:
        if importlib.util.find_spec("modal") is None:
            return False
    except (ImportError, ValueError):
        return False
    return bool(os.environ.get(_MODAL_TOKEN_ENV)) or _modal_toml_exists()


class ModalSandboxStrategy(SandboxStrategy):
    """Wraps argv in an ephemeral Modal cloud sandbox.

    Trust model: the wrapped command runs on Modal's infrastructure, fully
    isolated from the local host. The sandbox is created AND terminated
    inside this one ``run`` call (the ``e2b.py`` cross-event-loop pattern
    — the SDK's grpc client must not survive across event loops).
    """

    name = "modal"

    def __init__(self) -> None:
        # Capability probe cached at construction (parallels e2b / daytona).
        self._available = _modal_available()

    def is_available(self) -> bool:
        return self._available

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        del config  # Modal picks the image at create time; image arg n/a here.
        # Synthetic audit marker (the real call is a network request).
        return ["modal", "sandbox", "create", *argv]

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
            from modal import Sandbox
        except ImportError as exc:
            raise SandboxUnavailable(
                "modal strategy: the 'modal' package is not installed; "
                "install with `pip install opencomputer[modal]`"
            ) from exc

        if not (os.environ.get(_MODAL_TOKEN_ENV) or _modal_toml_exists()):
            raise SandboxUnavailable(
                "modal strategy: MODAL credentials not found — set "
                "MODAL_TOKEN_ID + MODAL_TOKEN_SECRET, or run `modal token "
                "set` to write ~/.modal.toml"
            )

        # Follow the approved warn-and-proceed; Modal exposes ``block_network``
        # on ``create.aio`` but M2 honors the approved pre-mortem (e2b /
        # daytona parity). Enforcing this is a noted follow-up.
        if not config.network_allowed:
            _log.warning(
                "modal strategy: network containment was requested "
                "(network_allowed=False) but is not enforced in M2 — the "
                "wrapped command WILL have outbound network access. Use a "
                "local strategy (docker / bwrap / sandbox-exec) to enforce "
                "network-deny today."
            )

        # Modal's create+wait pattern has no stdin channel: stdin can't be
        # fed to an already-running sandbox process.
        if stdin is not None:
            _log.warning(
                "modal strategy: stdin was supplied (%d bytes) but Modal's "
                "sandbox-as-entrypoint pattern has no stdin channel — the "
                "input will NOT reach the wrapped command. Use a local "
                "strategy if the command needs stdin.",
                len(stdin),
            )

        envs = filtered_env(config)
        wrapped = self.explain(argv, config=config)
        cap = config.cpu_seconds_limit
        start = time.monotonic()

        async def _create_wait_read() -> tuple[int | None, object, object]:
            # M-1: argv is varargs; pass positionally (no shlex.join).
            sandbox = await Sandbox.create.aio(
                *argv, env=envs, timeout=cap, workdir=cwd,
            )
            try:
                # M-3: wait returns the returncode (no exception on non-zero).
                returncode = await sandbox.wait.aio()
                # M-2: stdout / stderr captured separately — no 2>&1 wrap.
                stdout = await sandbox.stdout.read.aio()
                stderr = await sandbox.stderr.read.aio()
                return returncode, stdout, stderr
            finally:
                # Best-effort teardown. A failed terminate must not override
                # the command's result or exception.
                try:
                    await sandbox.terminate.aio()
                except Exception as exc:  # noqa: BLE001 — teardown best-effort
                    _log.warning(
                        "modal strategy: failed to terminate sandbox after "
                        "run: %s",
                        exc,
                    )

        try:
            returncode, stdout, stderr = await asyncio.wait_for(
                _create_wait_read(), timeout=cap,
            )
        except TimeoutError:
            # The whole create+wait+read overran cpu_seconds_limit. The inner
            # ``finally`` ran terminate already; nothing more to clean here.
            return SandboxResult(
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=TIMEOUT_STDERR,
                duration_seconds=time.monotonic() - start,
                wrapped_command=wrapped,
                strategy_name=self.name,
            )

        return SandboxResult(
            exit_code=_coerce_exit_code(returncode),
            stdout=decode_stream(stdout),
            stderr=decode_stream(stderr),
            duration_seconds=time.monotonic() - start,
            wrapped_command=wrapped,
            strategy_name=self.name,
        )


def _coerce_exit_code(value: object) -> int:
    """Best-effort coerce a Modal returncode to ``int``.

    ``Sandbox.wait`` returns ``int | None``; ``None`` (the process didn't
    set an exit code) maps to ``-1``, matching the host-process strategies'
    shape.
    """
    if isinstance(value, int):
        return value
    if value is None:
        return -1
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return -1
