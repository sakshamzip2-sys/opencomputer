"""opencli-bridge plugin — wraps @jackwener/opencli.

Three things happen at register-time:

  1. PATH prepend — make ``node_modules/.bin/opencli`` discoverable to the
     dispatcher's ``shutil.which`` lookup. Same trick browser-harness uses
     for its agent-browser binary.

  2. Extension side-load — append the bundled
     ``extension/v1.0.6/`` path to ``AGENT_BROWSER_EXTENSIONS`` so the
     OpenCLI Chrome extension auto-loads into the agent's own Chrome (the
     one browser-harness manages). Additive — agent-browser merges paths,
     doesn't replace, so this coexists with any user-supplied extensions.

  3. HOME-shim per OC profile — opencli hardcodes
     ``os.homedir() / ".opencli"`` (verified at
     ``node_modules/@jackwener/opencli/dist/src/main.js:29``). To get
     per-OC-profile state isolation without polluting the user's real
     ``~/.opencli/``, we point opencli's HOME at a shim dir under the
     active OC profile. The shim's ``.opencli`` symlinks to a real
     ``opencli/`` dir under the profile home (less hidden, more
     discoverable). Surgical override — opencli uses ``os.homedir()``
     only for the state path; nothing else needs to change.

Skills are registered through OC's bundled-skill loader if the API is
present. Falls back silently on older PluginAPI shapes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger("opencomputer.opencli_bridge.plugin")

_PLUGIN_DIR = Path(__file__).resolve().parent
_OC_NODE_BIN = _PLUGIN_DIR.parent.parent / "node_modules" / ".bin"
_BUNDLED_EXTENSION = _PLUGIN_DIR / "extension" / "v1.0.6"
_BUNDLED_SKILLS = _PLUGIN_DIR / "skills"


def _prepend_node_bin_to_path() -> None:
    """Make ``node_modules/.bin/opencli`` resolve via ``shutil.which``."""
    if not _OC_NODE_BIN.is_dir():
        return
    existing = os.environ.get("PATH", "").split(os.pathsep)
    if str(_OC_NODE_BIN) not in existing:
        os.environ["PATH"] = (
            str(_OC_NODE_BIN) + os.pathsep + os.environ.get("PATH", "")
        )


def _append_extension_path() -> None:
    """Side-load OpenCLI extension into the agent's Chrome.

    ``AGENT_BROWSER_EXTENSIONS`` is a comma-separated list. We append
    rather than overwrite so other plugins (or user-supplied extensions)
    survive.
    """
    if not _BUNDLED_EXTENSION.is_dir():
        _log.warning(
            "OpenCLI extension dir missing at %s; opencli browser commands "
            "will fail until the extension is bundled or installed manually",
            _BUNDLED_EXTENSION,
        )
        return
    bundled = str(_BUNDLED_EXTENSION)
    current = os.environ.get("AGENT_BROWSER_EXTENSIONS", "").strip()
    if not current:
        os.environ["AGENT_BROWSER_EXTENSIONS"] = bundled
    elif bundled not in [p.strip() for p in current.split(",")]:
        os.environ["AGENT_BROWSER_EXTENSIONS"] = current + "," + bundled


def _resolve_oc_profile_home() -> Path | None:
    """Resolve the active OC profile home, if available.

    Returns ``None`` if OC profile resolution fails — caller falls back
    to user's real ``~/.opencli`` (no isolation, but functional).
    """
    try:
        from opencomputer.agent.config import _home  # type: ignore[import-not-found]
        return Path(_home())
    except Exception as exc:  # noqa: BLE001
        _log.debug("OC profile home resolution failed: %s", exc)
        return None


def _setup_home_shim() -> None:
    """Create a HOME-shim dir whose ``.opencli`` points at OC-profile state.

    Layout::

        <oc_profile_home>/
        ├── opencli/                       ← REAL state (commands, configs, plugins)
        └── opencli-shim-home/
            └── .opencli  →  ../opencli    ← symlink the dispatcher's HOME points at

    Subprocess HOME is set to ``opencli-shim-home/`` per-call (in
    dispatcher.py). opencli's ``os.homedir() / ".opencli"`` then resolves
    to the real per-profile dir. User's real ``~/.opencli`` is untouched.
    """
    profile_home = _resolve_oc_profile_home()
    if profile_home is None:
        return
    real_state = profile_home / "opencli"
    shim_home = profile_home / "opencli-shim-home"
    shim_dot = shim_home / ".opencli"

    real_state.mkdir(parents=True, exist_ok=True)
    shim_home.mkdir(parents=True, exist_ok=True)

    # Create or repair the symlink. ``shim_dot`` may be missing, may be
    # a stale symlink (target dir got moved), or may be a real dir from
    # an old install — handle each.
    if shim_dot.is_symlink():
        try:
            target = os.readlink(shim_dot)
            if target != str(real_state) and Path(target) != real_state:
                shim_dot.unlink()
        except OSError:
            pass
    if not shim_dot.exists() and not shim_dot.is_symlink():
        try:
            os.symlink(real_state, shim_dot)
        except OSError as exc:
            _log.warning(
                "Could not create opencli HOME-shim symlink %s -> %s: %s",
                shim_dot,
                real_state,
                exc,
            )


# Eager PATH + extension wiring — must run at import so other plugins
# loaded after us see the merged ``AGENT_BROWSER_EXTENSIONS``.
_prepend_node_bin_to_path()
_append_extension_path()
_setup_home_shim()

# Sibling-module imports — the loader puts this dir on sys.path[0] and
# clears the conventional short-name module cache between plugin loads.
import tools  # type: ignore[import-not-found]  # noqa: E402


def register(api: Any) -> None:  # PluginAPI is duck-typed
    """Register the 5 OpenCLI tools + (optionally) skills + doctor row."""
    for tool_cls in tools.ALL_TOOL_CLASSES:
        try:
            api.register_tool(tool_cls())
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Failed to register opencli-bridge tool %s: %s",
                tool_cls.__name__,
                exc,
            )

    # Skill registration — older PluginAPI shapes don't expose this; we
    # fail open so the plugin still works without skills.
    if _BUNDLED_SKILLS.is_dir() and hasattr(api, "register_skill_dir"):
        try:
            api.register_skill_dir(_BUNDLED_SKILLS)
        except Exception as exc:  # noqa: BLE001
            _log.debug("register_skill_dir failed: %s", exc)

    # Doctor row — same fail-open pattern as browser-harness.
    if hasattr(api, "register_doctor_contribution"):
        try:
            from doctor import run as _doctor_run  # type: ignore[import-not-found]
            from plugin_sdk.doctor import HealthContribution  # type: ignore[import-not-found]
            api.register_doctor_contribution(
                HealthContribution(
                    id="opencli-bridge",
                    description=(
                        "opencli-bridge: opencli CLI + bundled extension + "
                        "daemon health"
                    ),
                    run=_doctor_run,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("opencli-bridge doctor not registered: %s", exc)


__all__ = ["register"]
