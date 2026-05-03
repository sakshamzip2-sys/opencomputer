"""Browser-control plugin — Playwright-based automation."""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.browser_control.plugin")


def register(api) -> None:  # noqa: ANN001
    """Register all browser tools (5 base + 6 Hermes-parity).

    Sibling import note: the loader inserts this plugin's directory at
    ``sys.path[0]`` before calling ``register``, so bare-name imports of
    sibling files resolve via the dir-on-sys.path mechanism. The siblings
    are renamed with a ``_`` prefix (``_tools``, ``_browser_session``)
    rather than the generic ``tools`` / ``browser`` to dodge sys.path
    races against other plugins that ship a top-level ``tools/`` package
    or ``browser.py`` (e.g. coding-harness's ``tools/`` subpackage).
    Without this discipline, the import non-deterministically lands in
    the wrong plugin and the silent ``except ImportError`` returns with
    zero tools registered — the exact bug that motivated this rename.
    """
    try:
        from _tools import ALL_TOOLS  # type: ignore[import-not-found]
        for tool_cls in ALL_TOOLS:
            try:
                api.register_tool(tool_cls())
            except Exception as exc:  # noqa: BLE001
                _log.warning("Failed to register %s: %s", tool_cls.__name__, exc)
    except ImportError as exc:
        _log.warning("browser-control tools not loadable: %s", exc)
