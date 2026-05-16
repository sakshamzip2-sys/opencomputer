"""E2B strategy — containment via an ephemeral E2B cloud sandbox.

Each call boots a transient E2B cloud VM (``AsyncSandbox.create``), runs
the wrapped command inside it (``sandbox.commands.run``), and tears the
sandbox down in a ``finally`` block (``sandbox.kill``). The sandbox is
created **and** killed inside this one ``run`` call — never cached on the
strategy instance — because ``AsyncSandbox`` is built on
``httpx.AsyncClient`` and must not be awaited across event loops (OC's
chat path does one ``asyncio.run`` per turn). See
``docs/refs/e2b/2026-05-16-sdk-survey.md`` for the full SDK survey.

Availability: the optional ``e2b`` package must import AND ``E2B_API_KEY``
must be set (``pip install opencomputer[e2b]``; key from
https://e2b.dev/dashboard). ``is_available()`` is cheap + cached and
never raises.

Survey mismatches handled here (numbered M-1…M-9 in the survey):

* **M-1 — argv vs command string.** OC's contract passes ``argv:
  list[str]``; E2B's ``commands.run`` takes a single shell-command
  string. We :func:`shlex.join` the argv before the call (the same
  pattern :mod:`opencomputer.sandbox.ssh` uses for its remote command).
* **M-3 — non-zero exit raises.** E2B raises ``CommandExitException`` on
  a non-zero exit code rather than returning a result. We catch it and
  synthesize a :class:`~plugin_sdk.SandboxResult` carrying the real
  ``exit_code`` from the exception, matching docker/none semantics.
* **M-6 — ``memory_mb_limit`` is template-defined.** E2B RAM is fixed by
  the sandbox template, not a per-call flag, so ``config.memory_mb_limit``
  is ignored (like macOS ``sandbox-exec``).
* **M-7 — ``network_allowed=False`` cannot be honored.** E2B sandboxes
  are cloud VMs that are always networked; there is no per-call
  network-deny switch. When the caller requests no network we log a
  WARNING that containment could not be enforced and proceed — we do
  **not** refuse the call (decided policy for M2).
* **M-8 — ``image`` does not carry over.** E2B selects a base image by
  *template id*, not a Docker image ref; ``config.image``
  (default ``"alpine:latest"``) is not a valid E2B template and is
  ignored — the sandbox uses E2B's base template.
* **M-9 — ABC clause 1 ("``asyncio.create_subprocess_exec``, never
  blocking ``subprocess``") is satisfied only in spirit.** This strategy
  does network I/O via ``AsyncSandbox`` rather than a local exec; the
  intent (don't block the event loop) holds because every E2B call is
  ``await``-ed. A future reader should not "fix" this into a subprocess.
"""

from __future__ import annotations

import asyncio
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

_log = logging.getLogger("opencomputer.sandbox.e2b")

#: Environment variable the E2B SDK reads for authentication. Issued from
#: the E2B dashboard; prefixed ``e2b_``.
_E2B_API_KEY_ENV = "E2B_API_KEY"

#: Buffer (seconds) added to the E2B sandbox lifetime on top of
#: ``config.cpu_seconds_limit``. The OC-side ``asyncio.wait_for`` is the
#: authoritative wall-clock cap (clause 3 of the ABC); the sandbox-level
#: ``timeout`` is a backstop so a sandbox can't outlive the call by much
#: if the OC-side guard is somehow bypassed. A few seconds of slack keeps
#: the backstop from racing the real cap.
_SANDBOX_TIMEOUT_BUFFER_SECONDS = 5


def _e2b_available() -> bool:
    """Return True iff the ``e2b`` package imports AND ``E2B_API_KEY`` is set.

    Cheap and side-effect-free: an ``importlib`` spec lookup (no import of
    the package body) plus an ``os.environ`` read. Never raises — any
    failure is reported as ``False``.
    """
    if not os.environ.get(_E2B_API_KEY_ENV):
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("e2b") is not None
    except (ImportError, ValueError):
        # ImportError: importlib machinery unavailable (never in practice).
        # ValueError: a partially-installed ``e2b`` with a broken spec.
        return False


class E2BSandboxStrategy(SandboxStrategy):
    """Wraps argv in an ephemeral E2B cloud sandbox.

    Trust model: the wrapped command runs on E2B's infrastructure, fully
    isolated from the local host — there is no local filesystem to touch.
    The trade-off (see M-7) is that the sandbox is always networked, so
    ``network_allowed=False`` is logged-and-ignored rather than enforced.
    """

    name = "e2b"

    def __init__(self) -> None:
        # Capability probe is cached at construction — mirrors
        # DockerStrategy.__init__'s ``docker info`` probe. The check is
        # cheap (spec lookup + env read) so re-running it would also be
        # fine; caching keeps ``is_available()`` constant-time and matches
        # the rest of the subpackage.
        self._available = _e2b_available()

    def is_available(self) -> bool:
        return self._available

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        """Return an audit-meaningful wrapped command without running it.

        E2B has no local argv to surface (the real call is a network
        request), so we return a synthetic marker argv: the ``e2b``
        invocation that would run the same command, with the joined
        command string as the trailing argument. ``run`` puts the
        identical list in :attr:`SandboxResult.wrapped_command`.
        """
        del config  # E2B base template is fixed; ``image`` does not apply.
        return ["e2b", "sandbox", "run", "--", shlex.join(argv)]

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        # Lazy import: a missing optional ``e2b`` dependency must never
        # crash OC at module-import time.
        try:
            from e2b import AsyncSandbox
        except ImportError as exc:
            raise SandboxUnavailable(
                "e2b strategy: the 'e2b' package is not installed; "
                "install with `pip install opencomputer[e2b]`"
            ) from exc
        # ``CommandExitException`` is the exception E2B raises on a
        # non-zero exit (M-3). The SDK re-exports it from the package
        # root; ``e2b.exceptions`` is the fallback path. Resolving it
        # path-resiliently means a future SDK reshuffle of the exception
        # module doesn't silently turn every failing command into an
        # unhandled error.
        CommandExitException = _resolve_command_exit_exception()

        if not os.environ.get(_E2B_API_KEY_ENV):
            raise SandboxUnavailable(
                "e2b strategy: E2B_API_KEY is not set; obtain a key from "
                "https://e2b.dev/dashboard and export E2B_API_KEY"
            )

        # M-7: E2B sandboxes are always networked — there is no per-call
        # network-deny switch. The default SandboxConfig requests
        # ``network_allowed=False``; honour the decided M2 policy — warn
        # loudly that containment could not be enforced, then proceed.
        if not config.network_allowed:
            _log.warning(
                "e2b strategy: network containment was requested "
                "(network_allowed=False) but E2B sandboxes are always "
                "networked — the wrapped command WILL have outbound "
                "network access. Use a local strategy (docker / bwrap / "
                "sandbox-exec) if network-deny must be enforced."
            )

        # E2B's ``commands.run`` has no stdin channel — the contract's
        # ``stdin`` argument cannot be delivered to the wrapped command.
        # Warn rather than drop it silently (the local strategies pipe
        # stdin via the subprocess); a caller that needs to feed input
        # should pick a local strategy.
        if stdin is not None:
            _log.warning(
                "e2b strategy: stdin was supplied (%d bytes) but E2B's "
                "command API has no stdin channel — the input will NOT "
                "reach the wrapped command. Use a local strategy if the "
                "command needs stdin.",
                len(stdin),
            )

        # M-1: E2B's commands.run takes a single shell-command string,
        # not an argv list. shlex.join produces a safely-quoted string.
        command = shlex.join(argv)
        # Env allowlist (clause 2 of the ABC): only config.allowed_env_vars
        # are forwarded into the sandbox via E2B's ``envs=``.
        envs = filtered_env(config)
        wrapped = self.explain(argv, config=config)

        # Wall-clock cap (clause 3 of the ABC). The whole create + run is
        # bounded by ``asyncio.wait_for`` — this OC-side guard is the
        # authoritative cap (docker / ssh enforce the cap the same way).
        # E2B's own ``timeout=`` kwargs are *server-side backstops* given
        # a few extra seconds of slack so the OC-side guard fires first
        # rather than racing them. The OC-side ``time.monotonic`` delta is
        # also the cost-guard's input (SandboxResult.duration_seconds).
        cap = config.cpu_seconds_limit
        backstop = cap + _SANDBOX_TIMEOUT_BUFFER_SECONDS
        start = time.monotonic()
        # Mutable one-slot holder so the inner coroutine can publish the
        # AsyncSandbox the instant ``create`` returns — the reference then
        # survives an ``asyncio.wait_for`` cancellation mid-``commands.run``
        # so the ``finally`` can always kill it.
        sandbox_slot: list[AsyncSandbox] = []

        async def _create_and_run() -> object:
            # M-8: no template id is passed — E2B uses its base template;
            # config.image is a Docker ref and does not apply here.
            sandbox = await AsyncSandbox.create(timeout=backstop, envs=envs)
            sandbox_slot.append(sandbox)
            return await sandbox.commands.run(
                command,
                cwd=cwd,
                envs=envs,
                timeout=backstop,
            )

        try:
            try:
                result = await asyncio.wait_for(_create_and_run(), timeout=cap)
            except CommandExitException as exc:
                # M-3: a non-zero exit raises rather than returning. The
                # exception carries the real exit_code / stdout / stderr —
                # synthesize the result so failing commands behave like
                # they do under docker / none (a result, not a raise).
                return SandboxResult(
                    exit_code=_coerce_exit_code(getattr(exc, "exit_code", None)),
                    stdout=decode_stream(getattr(exc, "stdout", "")),
                    stderr=decode_stream(getattr(exc, "stderr", "")),
                    duration_seconds=time.monotonic() - start,
                    wrapped_command=wrapped,
                    strategy_name=self.name,
                )
            return SandboxResult(
                exit_code=_coerce_exit_code(getattr(result, "exit_code", 0)),
                stdout=decode_stream(getattr(result, "stdout", "")),
                stderr=decode_stream(getattr(result, "stderr", "")),
                duration_seconds=time.monotonic() - start,
                wrapped_command=wrapped,
                strategy_name=self.name,
            )
        except TimeoutError:
            # The create + run overran ``config.cpu_seconds_limit``. The
            # ``finally`` below still kills the sandbox (if one was
            # created) so no cloud resource is left running.
            return SandboxResult(
                exit_code=TIMEOUT_EXIT_CODE,
                stdout="",
                stderr=TIMEOUT_STDERR,
                duration_seconds=time.monotonic() - start,
                wrapped_command=wrapped,
                strategy_name=self.name,
            )
        finally:
            # Always tear the sandbox down — E2B bills per running second,
            # and the auto-timeout may only *pause* (not kill) the
            # sandbox, so kill() is the only teardown guarantee. A failed
            # kill must not mask the real result/exception.
            if sandbox_slot:
                try:
                    await sandbox_slot[0].kill()
                except Exception as exc:  # noqa: BLE001 — teardown best-effort;
                    # a kill failure (network blip, already-gone sandbox)
                    # must never override the command's result.
                    _log.warning(
                        "e2b strategy: failed to kill sandbox after run: %s", exc
                    )


def _resolve_command_exit_exception() -> type[BaseException]:
    """Resolve E2B's ``CommandExitException`` class, path-resiliently.

    The class is raised by ``commands.run`` on a non-zero exit (M-3). The
    core ``e2b`` SDK re-exports it from the package root; ``e2b.exceptions``
    is the documented home. We try the root first, then the submodule.
    If neither is importable (a heavily-reshuffled SDK), we fall back to
    catching nothing exotic — :class:`Exception` — so a failing command
    is still mapped to a :class:`~plugin_sdk.SandboxResult` rather than
    crashing the loop; the broad fallback is the safe degradation.

    Caller has already confirmed ``e2b`` itself imports, so the package
    is present — only the *exception's location* is being probed here.
    """
    try:
        from e2b import CommandExitException  # type: ignore[attr-defined]

        return CommandExitException
    except ImportError:
        pass
    try:
        from e2b.exceptions import CommandExitException

        return CommandExitException
    except ImportError:
        # Last resort: the SDK is present but the exception type can't be
        # located. Catch broadly so a non-zero exit still yields a result.
        _log.warning(
            "e2b strategy: could not import CommandExitException from the "
            "'e2b' package; falling back to a broad Exception catch for "
            "non-zero-exit handling"
        )
        return Exception


def _coerce_exit_code(value: object) -> int:
    """Best-effort coerce an E2B exit code to ``int``.

    The SDK returns an ``int``; this guards against a ``None`` (defensive
    — e.g. a partial ``CommandExitException``) by mapping it to ``-1``,
    the same shape the host-process strategies use for an unknown code.
    """
    if isinstance(value, int):
        return value
    if value is None:
        return -1
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return -1
