"""Chrome launch — bootstrap, decorate, real spawn, readiness loop.

Implements the 13-step bootstrap-launch algorithm from
docs/refs/openclaw/browser/01-chrome-and-profiles.md (chrome.ts:305-457).

Public:
  RunningChrome                        handle returned by launch_openclaw_chrome.
  build_chrome_launch_args(...)        pure function — Chrome argv.
  resolve_openclaw_user_data_dir(...)  derives the per-profile user-data-dir.
  launch_openclaw_chrome(resolved, profile) -> RunningChrome (async)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..profiles.config import (
    DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    ResolvedBrowserConfig,
    ResolvedBrowserProfile,
)
from .decoration import (
    decorate_openclaw_profile,
    ensure_profile_clean_exit,
    is_profile_decorated,
)
from .executables import resolve_chrome_executable
from .lifecycle import is_chrome_reachable

_log = logging.getLogger("opencomputer.browser_control.chrome.launch")

# ─── tunable timeouts ─────────────────────────────────────────────────

CHROME_BOOTSTRAP_PREFS_TIMEOUT_MS = 10_000
CHROME_BOOTSTRAP_EXIT_TIMEOUT_MS = 5_000
CHROME_LAUNCH_READY_WINDOW_MS = 15_000
CHROME_LAUNCH_READY_POLL_MS = 200
CHROME_STDERR_HINT_MAX_CHARS = 2_000

_LOCAL_STATE = "Local State"
_DEFAULT_PREFS = "Default/Preferences"


# ─── data structures ─────────────────────────────────────────────────


@dataclass(slots=True)
class RunningChrome:
    pid: int
    executable: str
    user_data_dir: str
    cdp_port: int
    cdp_url: str
    started_at: float
    proc: asyncio.subprocess.Process | None
    stderr_tail: list[bytes] = field(default_factory=list)


class ChromeLaunchError(RuntimeError):
    """Raised when Chrome fails to come up within the readiness window."""


# ─── path resolution ─────────────────────────────────────────────────


def resolve_openclaw_user_data_dir(
    profile_name: str = DEFAULT_OPENCLAW_BROWSER_PROFILE_NAME,
    *,
    base_dir: str | os.PathLike[str] | None = None,
) -> str:
    """`<base>/<profile>/user-data`. Default base = `~/.opencomputer/browser`."""
    base = Path.home() / ".opencomputer" / "browser" if base_dir is None else Path(base_dir)
    return str(base / profile_name / "user-data")


# ─── argv builder ────────────────────────────────────────────────────


def build_chrome_launch_args(
    resolved: ResolvedBrowserConfig,
    profile: ResolvedBrowserProfile,
    user_data_dir: str,
    *,
    headless: bool | None = None,
) -> list[str]:
    """Construct the Chrome CLI argv. Pure — re-export tested by unit tests."""
    is_headless = resolved.headless if headless is None else headless

    # --disable-features list. Chrome only honors the last
    # --disable-features flag, so we accumulate features here and emit
    # a single flag below.
    disable_features = ["Translate", "MediaRouter"]

    args: list[str] = [
        f"--remote-debugging-port={profile.cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--password-store=basic",
    ]
    if is_headless:
        args.extend(["--headless=new", "--disable-gpu"])
    if resolved.no_sandbox:
        args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    if sys.platform.startswith("linux"):
        args.append("--disable-dev-shm-usage")

    # Wave 6 — Track 1: auto-load the OpenComputer Browser Control
    # extension into managed Chrome. Zero user action required.
    #
    # The extension lives at extensions/browser-control/extension/dist/.
    # If `dist/` doesn't exist (extension not yet built — `bash
    # extension/build.sh`), we silently skip adding the flag. This
    # keeps managed-Chrome launches working for users who haven't
    # built the extension yet (e.g. fresh checkout, no Node.js).
    #
    # Only baked into the `managed` driver — `existing-session` and
    # `control-extension` profiles use a different attach path.
    if profile.driver == "managed":
        ext_dist = Path(__file__).resolve().parent.parent / "extension" / "dist"
        if (ext_dist / "background.js").is_file():
            args.append(f"--load-extension={ext_dist}")
            # Chrome 137+ added an in-product warning for unpacked
            # extensions loaded via --load-extension; this disables it.
            disable_features.append("DisableLoadExtensionCommandLineSwitch")

    args.append(f"--disable-features={','.join(disable_features)}")
    args.extend(resolved.extra_args)
    return args


# ─── helpers ─────────────────────────────────────────────────────────


def _chrome_env() -> dict[str, str]:
    """Pass-through process env with HOME force-set so test/CI overrides don't leak."""
    return {**os.environ, "HOME": os.path.expanduser("~")}


def _needs_bootstrap(user_data_dir: str) -> bool:
    base = Path(user_data_dir)
    return not (base / _LOCAL_STATE).is_file() or not (base / _DEFAULT_PREFS).is_file()


async def _wait_for_prefs_files(user_data_dir: str, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(0.05, timeout_ms / 1000.0)
    base = Path(user_data_dir)
    while time.monotonic() < deadline:
        if (base / _LOCAL_STATE).is_file() and (base / _DEFAULT_PREFS).is_file():
            return True
        await asyncio.sleep(0.1)
    return False


async def _terminate_quietly(proc: asyncio.subprocess.Process, timeout_ms: int) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.05, timeout_ms / 1000.0))
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            _log.warning("_terminate_quietly: pid=%s ignored SIGKILL", proc.pid)


async def _await_chrome_ready(cdp_url: str, *, window_ms: int, poll_ms: int) -> bool:
    deadline = time.monotonic() + max(0.1, window_ms / 1000.0)
    poll_s = max(0.05, poll_ms / 1000.0)
    while time.monotonic() < deadline:
        if await is_chrome_reachable(cdp_url, timeout_ms=poll_ms + 200):
            return True
        await asyncio.sleep(poll_s)
    return False


async def _read_stderr_tail(
    proc: asyncio.subprocess.Process,
    sink: list[bytes],
) -> None:
    if proc.stderr is None:
        return
    while True:
        try:
            chunk = await proc.stderr.read(4096)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            return
        if not chunk:
            return
        sink.append(chunk)
        # Cap sink to last few buffers so a chatty Chrome doesn't OOM us.
        if len(sink) > 32:
            del sink[: len(sink) - 32]


# ─── launch ──────────────────────────────────────────────────────────


async def launch_openclaw_chrome(
    resolved: ResolvedBrowserConfig,
    profile: ResolvedBrowserProfile,
    *,
    user_data_dir: str | None = None,
    spawn: Any | None = None,
) -> RunningChrome:
    """Spawn Chrome and return when CDP is reachable.

    Steps mirror chrome.ts:305-457:
      1. Validate (loopback only, managed driver only).
      2. Resolve executable.
      3. Create user-data-dir.
      4. needs_bootstrap = !exists(Local State) || !exists(Default/Preferences).
      5. needs_decorate via is_profile_decorated.
      6. If needs_bootstrap: spawn -> wait for prefs files -> SIGTERM -> wait for exit.
      7. If needs_decorate: decorate_openclaw_profile (best-effort).
      8. ensure_profile_clean_exit.
      9. Real spawn.
     10. Poll is_chrome_reachable until true (15s window).
     11. On failure: kill + raise ChromeLaunchError with stderr hint.

    `spawn` defaults to `asyncio.create_subprocess_exec`; tests inject a fake.
    """
    if profile.driver == "existing-session":
        raise ChromeLaunchError(
            f"profile {profile.name!r} has driver='existing-session' — Chrome MCP attaches; "
            "this routine only manages managed-driver profiles"
        )
    if not profile.cdp_is_loopback:
        raise ChromeLaunchError(
            f"profile {profile.name!r} cdp_url is non-loopback ({profile.cdp_host!r}); "
            "remote profiles do not spawn Chrome locally"
        )

    spawn_fn = spawn or asyncio.create_subprocess_exec

    executable = resolved.executable_path or resolve_chrome_executable()
    if not executable:
        raise ChromeLaunchError("could not locate a Chrome / Chromium binary on this host")

    udir = user_data_dir or resolve_openclaw_user_data_dir(profile.name)
    Path(udir).mkdir(parents=True, exist_ok=True)

    needs_bootstrap = _needs_bootstrap(udir)
    needs_decorate = not is_profile_decorated(udir, profile.name, profile.color)

    if needs_bootstrap:
        bootstrap_args = build_chrome_launch_args(resolved, profile, udir)
        bootstrap_proc = await spawn_fn(
            executable,
            *bootstrap_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_chrome_env(),
        )
        try:
            ok = await _wait_for_prefs_files(udir, CHROME_BOOTSTRAP_PREFS_TIMEOUT_MS)
            if not ok:
                _log.warning(
                    "bootstrap timed out waiting for %s + %s in %s",
                    _LOCAL_STATE,
                    _DEFAULT_PREFS,
                    udir,
                )
        finally:
            await _terminate_quietly(bootstrap_proc, CHROME_BOOTSTRAP_EXIT_TIMEOUT_MS)
        needs_decorate = not is_profile_decorated(udir, profile.name, profile.color)

    if needs_decorate:
        decorate_openclaw_profile(udir, name=profile.name, color=profile.color)

    ensure_profile_clean_exit(udir)

    real_args = build_chrome_launch_args(resolved, profile, udir)
    proc = await spawn_fn(
        executable,
        *real_args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=_chrome_env(),
    )
    stderr_tail: list[bytes] = []
    stderr_task = asyncio.create_task(_read_stderr_tail(proc, stderr_tail))

    try:
        ready = await _await_chrome_ready(
            profile.cdp_url,
            window_ms=CHROME_LAUNCH_READY_WINDOW_MS,
            poll_ms=CHROME_LAUNCH_READY_POLL_MS,
        )
    except BaseException:
        stderr_task.cancel()
        await _terminate_quietly(proc, 1_000)
        raise

    if not ready:
        await _terminate_quietly(proc, 1_000)
        stderr_task.cancel()
        hint = b"".join(stderr_tail)[-CHROME_STDERR_HINT_MAX_CHARS:].decode("utf-8", errors="replace")
        suffix = f"\nstderr tail:\n{hint}" if hint else ""
        sandbox_hint = (
            "\nHint: Linux without --no-sandbox often fails when running as root or in a "
            "container; set browser.no_sandbox=true if applicable."
            if sys.platform.startswith("linux") and not resolved.no_sandbox
            else ""
        )
        raise ChromeLaunchError(
            f"Chrome did not become reachable within "
            f"{CHROME_LAUNCH_READY_WINDOW_MS}ms at {profile.cdp_url}.{suffix}{sandbox_hint}"
        )

    return RunningChrome(
        pid=proc.pid,
        executable=executable,
        user_data_dir=udir,
        cdp_port=profile.cdp_port,
        cdp_url=profile.cdp_url,
        started_at=time.time(),
        proc=proc,
        stderr_tail=stderr_tail,
    )
