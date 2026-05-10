"""subprocess wrapper for the ``opencli`` Node CLI.

Three responsibilities:

  1. Build the right argv for ``opencli <site> <command> [args] --json``
     or ``opencli browser <action> ...``.

  2. Run it under the HOME-shim so opencli's ``os.homedir() / ".opencli"``
     resolves to per-OC-profile state. Set up by ``plugin.py`` at register
     time; we just point HOME at it here.

  3. Parse stdout. opencli emits one of three shapes:
       a) ``--json`` flag → JSON document
       b) human mode → unstructured text
       c) failure → non-zero exit code + stderr message
     We always pass ``--json`` when feasible so callers get structured
     dicts. On non-JSON output we degrade to ``{"text": "<stdout>"}``.

The daemon (port 19825 by default) auto-starts on first opencli call
that needs the browser; we don't manage its lifecycle. atexit hook from
browser-harness's dispatcher already kills the agent's Chrome on
shutdown — which is what cleans up the daemon transitively (the daemon
exits when its WebSocket peers all disconnect).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_log = logging.getLogger("opencomputer.opencli_bridge.dispatcher")

#: When >0, the dispatcher caps each opencli invocation at this many
#: seconds. Long-running adapter authoring + verify can legitimately
#: take a while; we default to 90s and let callers override.
DEFAULT_TIMEOUT_SECONDS = 90.0


class OpenCliInvocationError(Exception):
    """Raised on non-zero exit when stderr doesn't parse as structured."""


def _resolve_opencli_binary() -> str:
    """Resolve the ``opencli`` binary path, raising clearly if missing."""
    found = shutil.which("opencli")
    if found:
        return found
    raise FileNotFoundError(
        "opencli CLI not on PATH. Run "
        "`cd OpenComputer && npm install` to install it project-locally."
    )


def _shim_home() -> str | None:
    """Path to the HOME-shim dir set up by ``plugin.py:_setup_home_shim``.

    Returns ``None`` if OC profile resolution failed — subprocess will
    inherit real HOME and read user's real ``~/.opencli/`` instead. That's
    a degraded but functional fallback.
    """
    try:
        from opencomputer.agent.config import _home  # type: ignore[import-not-found]
        candidate = Path(_home()) / "opencli-shim-home"
        if candidate.is_dir():
            return str(candidate)
    except Exception:  # noqa: BLE001
        pass
    return None


def _build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Compose subprocess env: real env + HOME shim + caller overrides."""
    env = {**os.environ}
    home_override = _shim_home()
    if home_override:
        env["HOME"] = home_override
    if extra:
        env.update(extra)
    return env


def run_opencli(
    args: list[str],
    *,
    timeout: float | None = None,
    json_mode: bool = True,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Invoke ``opencli`` with the given args; return parsed dict.

    ``args`` should NOT include the ``opencli`` program name itself.
    Pass e.g. ``["hackernews", "top", "--limit", "5"]``. We append
    ``--json`` automatically when ``json_mode=True`` and the args don't
    already include a format flag.

    On success: returns parsed JSON (or ``{"text": ...}`` if not JSON).
    On non-zero exit: returns ``{"error": ..., "stderr": ..., "exit": N}``.
    """
    bin_path = _resolve_opencli_binary()

    cmd = [bin_path, *args]
    if json_mode and not any(a in {"-f", "--format"} for a in args):
        cmd.extend(["-f", "json"])

    env = _build_env()
    timeout_s = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS

    _log.debug("opencli invocation: cmd=%s timeout=%ss", cmd, timeout_s)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": "timeout",
            "stderr": f"opencli {' '.join(args)} exceeded {timeout_s}s",
            "exit": -1,
        }
    except FileNotFoundError as exc:
        return {"error": "binary_not_found", "stderr": str(exc), "exit": -1}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        # opencli sometimes emits structured failure to stdout AND a
        # human message to stderr; prefer parsing stdout first.
        parsed = _try_parse_json(stdout)
        if isinstance(parsed, dict):
            parsed.setdefault("exit", proc.returncode)
            return parsed
        return {
            "error": "nonzero_exit",
            "stderr": stderr or stdout or f"exit={proc.returncode}",
            "exit": proc.returncode,
        }

    if json_mode:
        parsed = _try_parse_json(stdout)
        if parsed is not None:
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        # JSON-mode requested but stdout isn't JSON — surface as text so
        # the LLM still gets something useful.
        return {"text": stdout, "stderr": stderr}

    return {"text": stdout, "stderr": stderr}


def _try_parse_json(s: str) -> Any | None:
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # opencli sometimes prints warning lines (those "Failed to load
        # module ..." messages) before the JSON body. Try to recover by
        # finding the first ``{`` or ``[`` and parsing from there.
        for start_char in ("{", "["):
            idx = s.find(start_char)
            if idx > 0:
                try:
                    return json.loads(s[idx:])
                except json.JSONDecodeError:
                    continue
        return None


def run_browser(
    action: str,
    *,
    args: list[str] | None = None,
    profile: str | None = None,
    target: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """``opencli browser <action> [...args]`` shortcut.

    ``action`` is one of: ``open``, ``state``, ``click``, ``type``, ``fill``,
    ``select``, ``keys``, ``wait``, ``get``, ``find``, ``extract``, ``frames``,
    ``screenshot``, ``scroll``, ``back``, ``eval``, ``network``, ``tab``,
    ``init``, ``verify``, ``close``.
    """
    cmd_args = ["browser", action]
    if profile:
        cmd_args = ["--profile", profile] + cmd_args
    if target:
        cmd_args.extend(["--tab", target])
    if args:
        cmd_args.extend(args)
    return run_opencli(cmd_args, timeout=timeout)


def list_adapters() -> dict[str, Any]:
    """``opencli list --json`` — discover available adapters.

    Returns the raw opencli list payload. Callers typically project to
    ``{site: [command, ...]}`` for the LLM.
    """
    return run_opencli(["list"], timeout=15.0)


def doctor_check() -> dict[str, Any]:
    """``opencli doctor`` — check daemon + extension connectivity."""
    return run_opencli(["doctor"], timeout=20.0, json_mode=False)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "OpenCliInvocationError",
    "doctor_check",
    "list_adapters",
    "run_browser",
    "run_opencli",
]
