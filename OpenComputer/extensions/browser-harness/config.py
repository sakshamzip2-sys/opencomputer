"""Per-profile / per-environment backend selection for browser-harness.

The lifted Hermes dispatcher already supports six backends out of the box
(see ``browser.md`` in the Hermes docs). Selection priority (first match wins):

  1. ``OPENCOMPUTER_BROWSER_CDP_URL`` env var → connect to user's running
     Chrome via CDP. Sets ``BROWSERS_HERMES_CDP_URL`` for the dispatcher.
  2. ``CAMOFOX_URL`` env var → Camofox stealth backend.
  3. ``BROWSER_USE_API_KEY`` env var → Browser Use Cloud.
  4. ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID`` → Browserbase.
  5. ``FIRECRAWL_API_KEY`` → Firecrawl.
  6. Per-profile config flag ``browser.backend`` in OC's ``config.yaml``
     (when set to one of: ``browser-use``, ``browserbase``, ``firecrawl``,
     ``camofox``, ``user-chrome``, ``agent-browser``). Overrides the
     env-var auto-detection above.
  7. Default → ``agent-browser`` local Chromium.

The dispatcher reads selection via ``compat.read_raw_config`` /
``compat.load_config`` and the env vars directly — there is no central
"backend selector" function. This module exposes utilities for OC's
``opencomputer doctor`` and the adapter-runner integration to introspect
the current selection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BackendSelection:
    """Snapshot of which backend will be used given current env + config."""

    backend: str  # canonical name: "agent-browser", "browser-use", ...
    source: str  # "env:CAMOFOX_URL" / "config:browser.backend" / "default"
    detail: str = ""


# Order matters — first match wins.
_ENV_BACKENDS: tuple[tuple[str, str], ...] = (
    ("OPENCOMPUTER_BROWSER_CDP_URL", "user-chrome"),
    ("CAMOFOX_URL", "camofox"),
    ("BROWSER_USE_API_KEY", "browser-use"),
    ("BROWSERBASE_API_KEY", "browserbase"),
    ("FIRECRAWL_API_KEY", "firecrawl"),
)


def detect_backend() -> BackendSelection:
    """Return the current backend selection without invoking the dispatcher.

    Useful for diagnostic surfaces — ``opencomputer doctor`` and the
    adapter-runner shim to log which backend is active.
    """
    for env_key, backend_name in _ENV_BACKENDS:
        if os.environ.get(env_key):
            return BackendSelection(
                backend=backend_name,
                source=f"env:{env_key}",
                detail=f"set; routing through {backend_name}",
            )

    # Per-profile config override
    try:
        from compat import read_raw_config  # type: ignore[import-not-found]

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser") if isinstance(cfg, dict) else None
        if isinstance(browser_cfg, dict):
            chosen = browser_cfg.get("backend")
            if isinstance(chosen, str) and chosen.strip():
                return BackendSelection(
                    backend=chosen.strip().lower(),
                    source="config:browser.backend",
                    detail="from profile config.yaml",
                )
    except Exception:  # noqa: BLE001 — diagnostics must never crash
        pass

    return BackendSelection(
        backend="agent-browser",
        source="default",
        detail="local headless Chromium (no env / config override set)",
    )


def use_browser_harness_for_adapter_runner() -> bool:
    """Should ``adapter-runner`` route browser ops through browser-harness?

    Single switch: set ``OPENCOMPUTER_USE_BROWSER_HARNESS=1`` to opt in.
    When False, ``adapter-runner`` keeps using the legacy
    ``browser-control`` plugin (Playwright/CDP). Default is False until
    browser-harness has been validated against your real adapters.
    """
    return os.environ.get("OPENCOMPUTER_USE_BROWSER_HARNESS") == "1"


__all__ = [
    "BackendSelection",
    "detect_backend",
    "use_browser_harness_for_adapter_runner",
]
