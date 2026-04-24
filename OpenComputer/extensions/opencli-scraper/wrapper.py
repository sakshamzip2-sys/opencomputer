"""OpenCLI subprocess wrapper.

Provides ``OpenCLIWrapper`` — the single point of contact between Python
and the Node.js ``opencli`` binary. All subprocess spawning, timeout
handling, port assignment, version checking, and error-code mapping lives
here. Nothing in this module touches the network directly.

Error taxonomy (maps OpenCLI exit codes → typed exceptions):
    1   → OpenCLIError           (generic / unexpected)
    2   → OpenCLIError           (bad arguments)
    66  → OpenCLIError           (empty result)
    69  → OpenCLINetworkError    (browser connect / adapter load)
    75  → OpenCLITimeoutError    (browser command timed out)
    77  → OpenCLIAuthError       (not logged in)
    78  → OpenCLIError           (config error)
    130 → OpenCLIError           (interrupted)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

MIN_OPENCLI_VERSION = "1.7.0"


# ── Exceptions ─────────────────────────────────────────────────────────────────


class OpenCLIError(RuntimeError):
    """Raised when opencli exits with a non-zero code or produces invalid output."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class OpenCLINetworkError(OpenCLIError):
    """Raised on exit code 69 — browser/daemon connection failure or adapter load error."""


class OpenCLIAuthError(OpenCLIError):
    """Raised on exit code 77 — authentication required (login or cookie missing)."""


class OpenCLIRateLimitError(OpenCLIError):
    """Raised when the upstream site signals rate-limiting (detected in stderr)."""


class OpenCLITimeoutError(OpenCLIError):
    """Raised when the subprocess times out and is killed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=75)


class OpenCLIVersionError(OpenCLIError):
    """Raised when the installed opencli binary is too old."""


# ── Port helpers ───────────────────────────────────────────────────────────────


def _port_is_free(port: int) -> bool:
    """Return True if *port* is not in use on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ── Version helpers ────────────────────────────────────────────────────────────


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse 'X.Y.Z' into (X, Y, Z). Extra tokens are ignored."""
    parts = []
    for token in version_str.strip().split("."):
        digits = "".join(c for c in token if c.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


# ── Exit-code → exception mapping ─────────────────────────────────────────────

_EXIT_CODE_MAP: dict[int, type[OpenCLIError]] = {
    69: OpenCLINetworkError,
    75: OpenCLITimeoutError,
    77: OpenCLIAuthError,
}


def _exc_for_code(exit_code: int, stderr_text: str, default_msg: str) -> OpenCLIError:
    """Map an opencli exit code to the appropriate exception type."""
    exc_cls = _EXIT_CODE_MAP.get(exit_code, OpenCLIError)
    msg = f"{default_msg} (exit {exit_code})"
    if stderr_text.strip():
        msg = f"{msg}: {stderr_text.strip()[:500]}"
    if exc_cls is OpenCLITimeoutError:
        return OpenCLITimeoutError(msg)
    return exc_cls(msg, exit_code=exit_code)


# ── Main wrapper class ─────────────────────────────────────────────────────────


class OpenCLIWrapper:
    """Asyncio-based wrapper around the ``opencli`` CLI binary.

    Parameters
    ----------
    opencli_binary:
        Explicit path to the ``opencli`` binary. Defaults to ``"opencli"``
        (resolved via ``PATH`` at subprocess time).
    default_timeout_s:
        Per-call timeout in seconds. Individual calls can override via
        ``run(..., timeout_s=N)``. Default 60.
    """

    def __init__(
        self,
        *,
        opencli_binary: str | None = None,
        default_timeout_s: int = 60,
    ) -> None:
        self._binary = opencli_binary or "opencli"
        self._default_timeout_s = default_timeout_s
        # Global semaphore: cap concurrent scrapes at 8 (design §6 addendum).
        self._semaphore = asyncio.Semaphore(8)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def run(
        self,
        adapter: str,
        *args: str,
        timeout_s: int | None = None,
    ) -> dict:
        """Invoke ``opencli <adapter> <args…>`` and return parsed JSON output.

        Parameters
        ----------
        adapter:
            The OpenCLI adapter slug, e.g. ``"github/user"`` or
            ``"reddit/posts"``.
        *args:
            Positional arguments forwarded to the adapter CLI.
        timeout_s:
            Per-call timeout override. ``None`` uses ``self._default_timeout_s``.

        Returns
        -------
        dict
            Parsed JSON output from opencli. Always a dict (the top-level
            ``{ok, data}`` envelope or error envelope).

        Raises
        ------
        OpenCLITimeoutError
            Subprocess did not complete within ``timeout_s`` seconds.
        OpenCLINetworkError
            Exit code 69 — daemon/extension not reachable.
        OpenCLIAuthError
            Exit code 77 — authentication required.
        OpenCLIError
            Any other non-zero exit code.
        """
        timeout = timeout_s if timeout_s is not None else self._default_timeout_s

        async with self._semaphore:
            port = self._find_free_port()
            env = {**os.environ, "OPENCLI_DAEMON_PORT": str(port)}
            cmd = [self._binary, *adapter.split("/"), *args, "--format", "json"]

            log.debug("opencli spawn: %s (port=%d, timeout=%ds)", cmd, port, timeout)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except FileNotFoundError as exc:
                raise OpenCLIError(
                    f"opencli binary not found at {self._binary!r}. "
                    "Install with: npm install -g @jackwener/opencli"
                ) from exc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout),
                )
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                raise OpenCLITimeoutError(
                    f"opencli timed out after {timeout}s running adapter {adapter!r}"
                )

            # Encoding-safe decode — invalid bytes replaced rather than crash.
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            return_code = proc.returncode
            if return_code != 0:
                raise _exc_for_code(return_code, stderr_text, f"opencli failed for {adapter!r}")

            if not stdout_text.strip():
                return {}

            try:
                return json.loads(stdout_text)
            except json.JSONDecodeError as exc:
                log.warning("opencli output is not valid JSON: %s…", stdout_text[:200])
                raise OpenCLIError(
                    f"opencli returned non-JSON output for adapter {adapter!r}: {exc}"
                ) from exc

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _find_free_port(self) -> int:
        """Scan 19825-19899 for a free port; return 19825 if free, else first free.

        Raises ``OpenCLIError`` if the entire range is occupied (stale daemon
        processes or unusually high concurrency).
        """
        # Prefer the canonical OpenCLI default port.
        if _port_is_free(19825):
            return 19825
        for candidate in range(19826, 19900):
            if _port_is_free(candidate):
                return candidate
        raise OpenCLIError(
            "No free port found in 19825-19899. "
            "Check for stale opencli daemon processes: `opencli daemon stop`"
        )

    async def _check_version(self) -> str:
        """Run ``opencli --version``, parse the version string, and compare
        against ``MIN_OPENCLI_VERSION``.

        Returns the version string on success.

        Raises
        ------
        OpenCLIVersionError
            If the installed binary is older than ``MIN_OPENCLI_VERSION``.
        OpenCLIError
            If the binary is missing or returns unexpected output.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=10.0,
            )
        except FileNotFoundError as exc:
            raise OpenCLIError(
                f"opencli binary not found: {self._binary!r}. "
                "Install with: npm install -g @jackwener/opencli"
            ) from exc
        except TimeoutError as exc:
            raise OpenCLIVersionError("opencli --version timed out") from exc

        version_str = (stdout_bytes + stderr_bytes).decode("utf-8", errors="replace").strip()
        # Extract the first token that looks like X.Y.Z
        version_found: str | None = None
        for token in version_str.split():
            if token.count(".") >= 1 and token[0].isdigit():
                version_found = token
                break

        if not version_found:
            raise OpenCLIVersionError(
                f"Could not parse opencli version from output: {version_str!r}"
            )

        installed = _parse_version(version_found)
        required = _parse_version(MIN_OPENCLI_VERSION)
        if installed < required:
            raise OpenCLIVersionError(
                f"opencli {version_found} is too old. "
                f"Minimum required: {MIN_OPENCLI_VERSION}. "
                "Upgrade with: npm install -g @jackwener/opencli"
            )
        return version_found


__all__ = [
    "OpenCLIWrapper",
    "OpenCLIError",
    "OpenCLINetworkError",
    "OpenCLIAuthError",
    "OpenCLIRateLimitError",
    "OpenCLITimeoutError",
    "OpenCLIVersionError",
    "MIN_OPENCLI_VERSION",
]
