"""Docker container reuse pool — Milestone 3 (sandbox-provider-breadth).

A pooled container is a long-lived ``docker run -d --name oc-pool-<key>
... tail -f /dev/null`` — kept alive so repeated same-scope tool calls
``docker exec`` into it instead of minting a fresh ``--rm`` container each
time (the historical per-call behavior).

Docker itself is the registry: pooled containers carry the ``oc-pool-``
name prefix, discoverable via ``docker ps --filter name=oc-pool-`` — there
is no separate persisted state file (so registry/reality drift is designed
out). This object holds only per-key :class:`asyncio.Lock` instances to
serialize concurrent create.

``acquire`` is the sole public method; container *cleanup* (``oc sandbox
list`` / ``prune`` + a reaper) is Milestone 4.
"""

from __future__ import annotations

import asyncio
import logging

from opencomputer.sandbox._common import decode_stream
from plugin_sdk.sandbox import SandboxUnavailable

_log = logging.getLogger("opencomputer.sandbox.pool")

#: Name prefix for every pooled container — the ``docker ps`` filter token.
_POOL_NAME_PREFIX = "oc-pool-"

#: Keepalive command. ``tail -f /dev/null`` blocks forever and is present
#: in every base image (busybox included) — unlike ``sleep infinity``,
#: which is GNU-coreutils-only and errors under Alpine's busybox ``sleep``.
_KEEPALIVE_ARGV: tuple[str, ...] = ("tail", "-f", "/dev/null")


class ContainerPool:
    """Reuses long-lived Docker containers keyed on a pool key.

    One :class:`ContainerPool` per process (the Docker strategy holds a
    module-level singleton). Cross-process safety rides on Docker's
    container-name uniqueness — a name collision on create surfaces as a
    non-zero ``docker run`` and the next ``acquire`` re-probes and attaches.
    """

    def __init__(self) -> None:
        # Per-pool-key locks. Serializes concurrent acquire() of the SAME
        # key so two tasks can't both create the container; different keys
        # never contend.
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def container_name(pool_key: str) -> str:
        """The deterministic container name for ``pool_key``."""
        return f"{_POOL_NAME_PREFIX}{pool_key}"

    def _lock_for(self, pool_key: str) -> asyncio.Lock:
        """Return (creating if needed) the lock for ``pool_key``.

        Safe under asyncio's cooperative scheduling: there is no ``await``
        between the ``get`` and the ``__setitem__``, so two tasks cannot
        race to install two different locks for one key.
        """
        lock = self._locks.get(pool_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[pool_key] = lock
        return lock

    async def acquire(
        self, pool_key: str, *, image: str, run_flags: list[str]
    ) -> str:
        """Return the name of a running pooled container for ``pool_key``.

        Reuses the container if it is already running; (re)creates it
        otherwise — ``docker run -d --name oc-pool-<key> <run_flags>
        <image> tail -f /dev/null``. Raises :class:`SandboxUnavailable` if
        creation fails (the Docker strategy maps that to the
        ``sandbox.fallback`` policy, exactly like an unreachable backend).

        ``run_flags`` are the containment flags (cap-drop, ``--memory``,
        ``--network``, bind mounts, …) the Docker strategy would otherwise
        pass to a transient ``docker run``. They are fixed at the pooled
        container's creation; the caller guarantees every ``acquire`` for
        one ``pool_key`` passes equivalent flags (the pool key embeds a
        digest of the containment config — see ``docker.py``).
        """
        name = self.container_name(pool_key)
        async with self._lock_for(pool_key):
            if await self._is_running(name):
                _log.debug("sandbox pool: reusing container %s", name)
                return name
            # Not running — drop any stopped husk with this name (else the
            # ``docker run --name`` below collides), then create fresh.
            await self._remove_if_exists(name)
            await self._create(name, image=image, run_flags=run_flags)
            _log.debug("sandbox pool: created container %s", name)
            return name

    async def _is_running(self, name: str) -> bool:
        """True iff a container named ``name`` exists AND is running."""
        rc, out, _ = await self._docker(
            "inspect", "-f", "{{.State.Running}}", name
        )
        return rc == 0 and out.strip() == "true"

    async def _remove_if_exists(self, name: str) -> None:
        """Force-remove ``name`` if present. A miss (no such container) is fine."""
        rc, _, err = await self._docker("rm", "-f", name)
        if rc != 0:
            # The common case — no container to remove — is not an error.
            _log.debug(
                "sandbox pool: `docker rm -f %s` rc=%d (%s)",
                name,
                rc,
                err.strip(),
            )

    async def _create(
        self, name: str, *, image: str, run_flags: list[str]
    ) -> None:
        """``docker run -d`` a keepalive container; raise on failure."""
        rc, _, err = await self._docker(
            "run", "-d", "--name", name, *run_flags, image, *_KEEPALIVE_ARGV
        )
        if rc != 0:
            raise SandboxUnavailable(
                f"docker pool: failed to create pooled container {name!r} "
                f"(rc={rc}): {err.strip()}"
            )

    async def _docker(self, *args: str) -> tuple[int, str, str]:
        """Run ``docker <args>``; return ``(returncode, stdout, stderr)``.

        The single subprocess seam — unit tests replace this with an
        in-memory fake so the pool's lifecycle logic is exercised without
        a Docker daemon.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, decode_stream(out), decode_stream(err)
