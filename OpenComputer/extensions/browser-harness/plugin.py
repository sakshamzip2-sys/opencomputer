"""browser-harness plugin — multi-backend browser automation.

Lifts Hermes Agent's browser tooling so OC inherits all backends day one:

  - Local Chromium via agent-browser CLI (default fallback)
  - User's real Chrome via CDP (``OPENCOMPUTER_BROWSER_CDP_URL``)
  - Browser Use Cloud (BROWSER_USE_API_KEY)
  - Browserbase (BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID)
  - Firecrawl (FIRECRAWL_API_KEY)
  - Camofox local stealth (CAMOFOX_URL)

The lifted dispatcher (``browser_tool.py``), Camofox client
(``browser_camofox*.py``), redaction module (``redact.py``), and cloud
providers (``browser_providers/*``) stay byte-identical to Hermes upstream
EXCEPT for top-of-file imports, which go through ``compat.py`` shims.
``tools.py`` is where OC's ``BaseTool`` contract meets the lifted code.

See ``VENDORED.md`` for provenance and re-sync notes.
"""

from __future__ import annotations

import os
from pathlib import Path

# Make the project-local ``agent-browser`` CLI (installed via
# ``npm install agent-browser`` at the OC repo root) discoverable to the
# lifted Hermes dispatcher. Hermes computes ``repo_root = parent.parent``
# of its ``browser_tool.py`` to locate ``node_modules/.bin/agent-browser``,
# which works for Hermes's flat repo (``hermes-agent/tools/browser_tool.py``)
# but lands one level too high for OC's plugin layout
# (``OpenComputer/extensions/browser-harness/browser_tool.py``). Rather
# than diverge from upstream by patching the dispatcher's discovery code,
# we just prepend OC's ``node_modules/.bin`` to ``PATH`` here so the
# dispatcher's first check (``shutil.which('agent-browser')``) succeeds.
# Idempotent — adds the path only once per process.
_PLUGIN_DIR = Path(__file__).resolve().parent
_OC_NODE_BIN = _PLUGIN_DIR.parent.parent / "node_modules" / ".bin"
if _OC_NODE_BIN.is_dir():
    _existing = os.environ.get("PATH", "").split(os.pathsep)
    if str(_OC_NODE_BIN) not in _existing:
        os.environ["PATH"] = (
            str(_OC_NODE_BIN) + os.pathsep + os.environ.get("PATH", "")
        )

# Sibling-module import — the OC plugin loader inserts this directory on
# ``sys.path`` and clears the common short-name module cache before each
# load (``provider``, ``adapter``, ``plugin``, ``hooks``, ``handlers``).
# ``tools`` isn't in that clear list — but it's still safe here because
# this is the only plugin that defines a top-level ``tools`` module.
import tools  # type: ignore[import-not-found]  # noqa: E402 — must come after PATH setup


def register(api) -> None:  # PluginAPI is duck-typed
    """Register browser-harness tools with the agent loop.

    All five wrappers are stateless adapters around the lifted dispatcher
    functions. Browser session lifecycle (per-task tabs, cleanup-on-idle)
    is owned by the dispatcher itself (and the ``agent-browser`` daemon
    underneath).

    Default browser profile is persistent and scoped per OC profile —
    ``<profile_home>/browser-profile/`` becomes the agent-browser
    user-data-dir. Cookies, logins, extensions, and history persist across
    runs (OpenClaw-style dedicated browser-state). Each ``-p <name>`` OC
    profile gets its own isolated browser profile. Users who already
    export ``AGENT_BROWSER_PROFILE`` are left alone.
    """
    if "AGENT_BROWSER_PROFILE" not in os.environ:
        try:
            from compat import get_hermes_home  # type: ignore[import-not-found]
            browser_profile_dir = get_hermes_home() / "browser-profile"
            browser_profile_dir.mkdir(parents=True, exist_ok=True)
            os.environ["AGENT_BROWSER_PROFILE"] = str(browser_profile_dir)
        except Exception:
            pass

    for tool_cls in tools.ALL_TOOL_CLASSES:
        api.register_tool(tool_cls())
