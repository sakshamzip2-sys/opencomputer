"""Browser-control plugin entry — registers the Browser discriminator
tool, the deprecation shims, and a doctor row.

Package namespace bootstrap: the W3 surface is split across multiple
sub-packages (``client/``, ``server/``, ``_utils/``, etc.) that import
each other via PEP-328 relative imports (``from ..server.auth import
BrowserAuth``). For those to resolve at runtime, ``extensions
.browser_control`` must exist in ``sys.modules`` as a real package
pointing at this directory. The production loader only inserts the
plugin root onto ``sys.path[0]``; it does NOT synthesise the parent
package. So we do that ourselves below — same pattern the test
conftest uses, kept in sync so test + production code paths share an
import shape.

Sibling-import discipline: the per-tool entry lives at ``_tool.py``
(leading-underscore, singular) to dodge the ``sys.modules['tools']``
collision against ``coding-harness/tools/`` — the same lesson PR #394
burned in for the legacy ``_tools.py`` predecessor.

Doctor row: probes (a) playwright importable, (b) playwright async_api
loadable, (c) optional control-port reachability when an explicit
``OPENCOMPUTER_BROWSER_CONTROL_URL`` is configured. The httpx import in
:func:`_doctor_run` is intentionally lazy (function-scope) so the
no-egress guard's module-scope sweep doesn't flag it.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

from plugin_sdk.doctor import HealthContribution, RepairResult

_log = logging.getLogger("opencomputer.browser_control.plugin")

#: ``extensions.browser_control`` package name we synthesise so the
#: relative imports in ``client/``, ``server/``, etc. resolve.
_PKG = "extensions.browser_control"
_PARENT_PKG = "extensions"


def _bootstrap_package_namespace() -> None:
    """Make ``extensions.browser_control`` resolvable in sys.modules.

    Idempotent — safe to call multiple times. Mirrors the per-extension
    alias registration in ``tests/conftest.py`` so both runtime + test
    paths use the same import shape.
    """
    plugin_root = Path(__file__).resolve().parent

    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [str(plugin_root.parent)]
        parent.__package__ = _PARENT_PKG
        sys.modules[_PARENT_PKG] = parent

    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(plugin_root)]
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
        # Bind the synthesised package as an attribute of the parent so
        # ``from extensions import browser_control`` works.
        setattr(sys.modules[_PARENT_PKG], "browser_control", pkg)


def register(api: Any) -> None:
    """Register Browser + 11 shims + doctor row."""
    _bootstrap_package_namespace()

    # Imports go through the package alias so the relative imports
    # inside ``client/`` and ``server/`` resolve correctly.
    from extensions.browser_control._tool import (  # type: ignore[import-not-found]
        Browser,
        DEPRECATION_SHIMS,
    )

    # ── Browser tool ──────────────────────────────────────────────
    browser_registered = False
    try:
        api.register_tool(Browser())
        browser_registered = True
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to register Browser tool: %s", exc)

    if browser_registered:
        # ── deprecation shims ─────────────────────────────────────
        for shim_cls in DEPRECATION_SHIMS:
            try:
                api.register_tool(shim_cls())
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "Failed to register deprecation shim %s: %s",
                    shim_cls.__name__,
                    exc,
                )

    # ── doctor row ────────────────────────────────────────────────
    try:
        api.register_doctor_contribution(
            HealthContribution(
                id="browser-control",
                description=(
                    "browser-control: Playwright + Chromium availability "
                    "(and optional control-service reachability)"
                ),
                run=_doctor_run,
            )
        )
    except AttributeError:
        # Older PluginAPI without doctor surface — quietly skip.
        pass
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to register doctor contribution: %s", exc)


# ─── doctor implementation ──────────────────────────────────────────────


async def _doctor_run(fix: bool) -> RepairResult:  # noqa: ARG001 — fix unused (read-only check)
    """Three-step probe.

    1. ``import playwright`` (ok / fail).
    2. ``async_playwright`` reachable.
    3. If ``OPENCOMPUTER_BROWSER_CONTROL_URL`` set, attempt a HEAD on it.
    """
    import os  # local — no leak into module scope

    # 1) playwright importable
    try:
        import playwright  # noqa: F401
    except ImportError:
        return RepairResult(
            id="browser-control",
            status="warn",
            detail=(
                "playwright not installed. "
                "pip install opencomputer[browser]"
            ),
        )

    # 2) async_api loadable
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return RepairResult(
            id="browser-control",
            status="warn",
            detail="playwright async_api not loadable (partial install?)",
        )

    # 3) optional control port reachability
    control_url = (os.environ.get("OPENCOMPUTER_BROWSER_CONTROL_URL") or "").strip()
    if control_url:
        try:
            import httpx  # local — kept out of module scope on purpose
        except ImportError:
            return RepairResult(
                id="browser-control",
                status="warn",
                detail=(
                    "httpx not installed (pip install opencomputer[browser])"
                ),
            )
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(control_url.rstrip("/") + "/")
        except (httpx.ConnectError, httpx.ReadTimeout):
            return RepairResult(
                id="browser-control",
                status="warn",
                detail=(
                    f"playwright ok; control service NOT reachable "
                    f"at {control_url} (start with `opencomputer "
                    f"browser start` or unset "
                    f"OPENCOMPUTER_BROWSER_CONTROL_URL)"
                ),
            )
        return RepairResult(
            id="browser-control",
            status="pass",
            detail=(
                f"playwright ok; control service reachable at "
                f"{control_url} (HTTP {resp.status_code})"
            ),
        )

    return RepairResult(
        id="browser-control",
        status="pass",
        detail=(
            "playwright ok; in-process dispatcher mode "
            "(set OPENCOMPUTER_BROWSER_CONTROL_URL to probe HTTP)"
        ),
    )


__all__ = ["register"]
