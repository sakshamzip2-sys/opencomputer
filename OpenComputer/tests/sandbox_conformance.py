"""SandboxBackend conformance harness — Milestone 1 (sandbox-provider-breadth).

``FakeSandboxBackend`` is an always-available, in-memory reference backend
so the conformance suite (``tests/test_sandbox_conformance.py``) genuinely
runs on every CI host — no Docker daemon, no platform sandbox, no cloud
credentials required.

``assert_conforms`` drives a backend through a fixed probe set and asserts
the :class:`plugin_sdk.SandboxBackend` contract. Every sandbox backend —
the shipped strategies and the M2 Daytona / Modal additions — is expected
to pass it; M2's cloud backends call it against a mocked SDK.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import time

from opencomputer.sandbox._common import (
    TIMEOUT_EXIT_CODE,
    TIMEOUT_STDERR,
    filtered_env,
)
from plugin_sdk.sandbox import SandboxBackend, SandboxConfig, SandboxResult

# --- Probe commands -------------------------------------------------------
# Each probe is BOTH a real shell command (so the shipped backends run it
# natively) AND recognised by FakeSandboxBackend's in-memory interpreter.

#: Env var ``assert_conforms`` plants in ``os.environ`` for the env-strip
#: probe — present in the parent env, absent from the probe's
#: ``allowed_env_vars``. A conformant backend must not leak it (ABC clause 2).
LEAK_ENV_VAR = "SANDBOX_CONFORMANCE_LEAK"
_LEAK_VALUE = "leaked-secret-value"

_PROBE_STDOUT = ("sh", "-c", "echo conformance-stdout-ok")
_PROBE_STDERR = ("sh", "-c", "echo conformance-stderr-ok 1>&2")
_PROBE_EXIT7 = ("sh", "-c", "exit 7")
_PROBE_SLEEP = ("sh", "-c", "sleep 30")
_PROBE_LEAK = ("printenv", LEAK_ENV_VAR)
_PROBE_ENV_OK = ("printenv", "PATH")


class FakeSandboxBackend(SandboxBackend):
    """In-memory reference backend — always available, spawns no process.

    Interprets the small probe vocabulary ``assert_conforms`` uses
    (``sh -c "echo … | exit … | sleep …"`` and bare ``printenv``) so the
    contract can be exercised with zero host dependencies. A TEST DOUBLE
    only: never registered as a real strategy, never added to
    ``SandboxStrategyName``.
    """

    name = "fake"

    def is_available(self) -> bool:
        return True

    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        del config  # the fake applies no per-config wrapping; argv passes through
        return list(argv)

    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        # stdin / cwd are part of the ABC signature; an in-memory test double
        # has no subprocess to feed them to.
        del stdin, cwd
        # Genuinely async — yields to the loop, never blocks it (ABC clause 1
        # in spirit; a test double has no subprocess to spawn).
        await asyncio.sleep(0)
        start = time.monotonic()
        exit_code, stdout, stderr = interpret_probe(
            argv,
            env=filtered_env(config),
            cpu_seconds_limit=config.cpu_seconds_limit,
        )
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - start,
            wrapped_command=list(argv),
            strategy_name=self.name,
        )


def interpret_probe(
    argv: list[str], *, env: dict[str, str], cpu_seconds_limit: int
) -> tuple[int, str, str]:
    """Map a probe argv to ``(exit_code, stdout, stderr)`` — purely in-memory.

    Public so M2's cloud-backend test mocks (Daytona, Modal) can delegate to
    the same probe-vocabulary interpreter as ``FakeSandboxBackend``. The
    backend supplies its already-filtered ``env`` dict + the ``cpu_seconds_limit``
    so the mock makes the same env / timeout decisions a real backend would.
    """
    tokens = _probe_tokens(argv)
    if not tokens:
        return 0, "", ""
    head = tokens[0]
    if head == "printenv":
        var = tokens[1] if len(tokens) > 1 else ""
        value = env.get(var, "")
        # printenv prints ``value\n`` + exit 0 when set; exit 1 when unset.
        return (0, f"{value}\n", "") if value else (1, "", "")
    if head == "echo":
        rest = list(tokens[1:])
        to_stderr = bool(rest) and rest[-1] in ("1>&2", ">&2")
        if to_stderr:
            rest = rest[:-1]
        line = " ".join(rest) + "\n"
        return (0, "", line) if to_stderr else (0, line, "")
    if head == "exit":
        try:
            return int(tokens[1]), "", ""
        except (IndexError, ValueError):
            return 0, "", ""
    if head == "sleep":
        try:
            seconds = float(tokens[1])
        except (IndexError, ValueError):
            seconds = 0.0
        if seconds > cpu_seconds_limit:
            return TIMEOUT_EXIT_CODE, "", TIMEOUT_STDERR
        return 0, "", ""
    # Any probe outside the conformance vocabulary: benign success. The fake
    # is a test double exercised only by assert_conforms's fixed probe set.
    return 0, "", ""


def _probe_tokens(argv: list[str]) -> list[str]:
    """Flatten ``sh -c "<script>"`` to its tokens; pass other argv through."""
    if (
        len(argv) >= 3
        and argv[0] in ("sh", "/bin/sh", "bash", "/bin/bash")
        and argv[1] == "-c"
    ):
        try:
            return shlex.split(argv[2])
        except ValueError:
            return []
    return list(argv)


def docker_probe_ready() -> bool:
    """True iff Docker is usable AND the conformance probe image is present.

    ``DockerStrategy`` does not auto-pull; a docker-available host that has
    never pulled ``alpine:latest`` would *fail* the probe rather than skip,
    so the docker conformance test gates on this.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "alpine:latest"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def assert_conforms(backend: SandboxBackend) -> None:
    """Assert ``backend`` satisfies the :class:`SandboxBackend` contract.

    Raises :class:`AssertionError` on the first violation. Covers ``name``,
    ``is_available()``, ``explain()`` shape, stdout/stderr capture,
    exit-code fidelity, the timeout sentinel (ABC clause 3), and
    env-allowlist stripping (ABC clause 2).
    """
    name = backend.name
    assert isinstance(name, str) and name, (
        f"backend.name must be a non-empty str, got {name!r}"
    )

    available = backend.is_available()
    assert isinstance(available, bool), (
        f"{name}.is_available() must return bool, got {type(available).__name__}"
    )

    explained = backend.explain(["echo", "hi"], config=SandboxConfig())
    assert isinstance(explained, list) and explained, (
        f"{name}.explain() must return a non-empty list"
    )
    assert all(isinstance(token, str) for token in explained), (
        f"{name}.explain() must return list[str]"
    )

    asyncio.run(_run_behavioural_probes(backend, name))


async def _run_behavioural_probes(backend: SandboxBackend, name: str) -> None:
    """The async half of :func:`assert_conforms` — drives ``backend.run``."""
    # stdout capture + result shape + strategy_name (ABC clause 4).
    result = await backend.run(list(_PROBE_STDOUT), config=SandboxConfig())
    assert isinstance(result, SandboxResult), (
        f"{name}.run() must return a SandboxResult, got {type(result).__name__}"
    )
    assert result.exit_code == 0, (
        f"{name}: stdout probe exit_code {result.exit_code} != 0 "
        f"(stderr: {result.stderr!r})"
    )
    assert "conformance-stdout-ok" in result.stdout, (
        f"{name}: stdout not captured (got {result.stdout!r})"
    )
    assert result.strategy_name == name, (
        f"{name}: strategy_name {result.strategy_name!r} != {name!r} "
        "(ABC clause 4)"
    )
    assert isinstance(result.wrapped_command, list), (
        f"{name}: wrapped_command must be a list"
    )

    # stderr capture. Lenient — some cloud backends (e.g. Daytona's
    # ``process.exec`` only captures stdout, verified from SDK source) merge
    # stderr into stdout via a ``2>&1`` wrap. Accept the marker in either
    # stream; a backend that drops the output entirely still fails.
    result = await backend.run(list(_PROBE_STDERR), config=SandboxConfig())
    assert (
        "conformance-stderr-ok" in result.stderr
        or "conformance-stderr-ok" in result.stdout
    ), (
        f"{name}: stderr probe output not captured "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )

    # Exit-code fidelity — a non-zero child exit must reach the result.
    result = await backend.run(list(_PROBE_EXIT7), config=SandboxConfig())
    assert result.exit_code == 7, (
        f"{name}: child exit code not propagated ({result.exit_code} != 7)"
    )

    # Timeout sentinel — ABC clause 3.
    result = await backend.run(
        list(_PROBE_SLEEP), config=SandboxConfig(cpu_seconds_limit=1)
    )
    assert result.exit_code == TIMEOUT_EXIT_CODE, (
        f"{name}: timeout exit_code {result.exit_code} != {TIMEOUT_EXIT_CODE}"
    )
    # Lenient on the sentinel's stream, for the same reason as stderr above.
    assert (
        TIMEOUT_STDERR in result.stderr or TIMEOUT_STDERR in result.stdout
    ), (
        f"{name}: timeout sentinel {TIMEOUT_STDERR!r} not surfaced "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )

    # Env allowlist — ABC clause 2. An allowlisted var passes through; a
    # parent-env var absent from allowed_env_vars must NOT reach the child.
    result = await backend.run(list(_PROBE_ENV_OK), config=SandboxConfig())
    assert result.stdout.strip(), (
        f"{name}: an allowlisted env var (PATH) did not pass through"
    )

    os.environ[LEAK_ENV_VAR] = _LEAK_VALUE
    try:
        result = await backend.run(
            list(_PROBE_LEAK),
            config=SandboxConfig(allowed_env_vars=("PATH",)),
        )
    finally:
        os.environ.pop(LEAK_ENV_VAR, None)
    assert _LEAK_VALUE not in result.stdout, (
        f"{name}: leaked a non-allowlisted env var into the sandbox "
        f"(ABC clause 2) — got {result.stdout!r}"
    )
