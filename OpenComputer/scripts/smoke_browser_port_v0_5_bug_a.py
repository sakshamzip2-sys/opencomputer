"""Smoke test for Browser-port v0.5 Bug A — adapter ctx lazy-bootstrap.

Verifies that an adapter's ``ctx.fetch_in_page(...)`` (or any other
``BrowserActions`` method) works WITHOUT the Browser tool having been
called first — i.e. the in-process dispatcher app gets lazy-built
inside ``client/fetch.py:fetch_browser_json`` regardless of the entry
point.

Pre-Bug-A: this script raised
    BrowserServiceError: In-process dispatcher is not registered ...

Post-Bug-A: the script reaches the dispatcher transport without that
error. The actual Browser action will fail because no real Chrome is
attached, but the failure mode must NOT be the bootstrap error.

Usage::

    cd OpenComputer
    .venv/bin/python scripts/smoke_browser_port_v0_5_bug_a.py
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path


async def _smoke_fetch_browser_json_path() -> bool:
    """Direct ``fetch_browser_json`` smoke — the bedrock of Bug A."""
    from extensions.browser_control._dispatcher_bootstrap import reset_for_tests
    from extensions.browser_control.client.fetch import (
        fetch_browser_json,
        get_default_dispatcher_app,
    )

    reset_for_tests()
    if get_default_dispatcher_app() is not None:
        print("  [setup] dispatcher slot already populated — reset failed")
        return False

    print("  [setup] dispatcher slot is empty (good)")
    body = await fetch_browser_json("GET", "/", timeout=5.0)

    if get_default_dispatcher_app() is None:
        print("  [fail] dispatcher slot is STILL empty after fetch_browser_json")
        return False

    if not isinstance(body, dict) or "status" not in body:
        print(f"  [fail] unexpected body: {body!r}")
        return False

    print(f"  [ok] body keys: {sorted(body.keys())}")
    return True


async def _smoke_adapter_ctx_path() -> bool:
    """End-to-end: build an AdapterContext, call ``ctx.fetch_in_page``,
    confirm the bootstrap error is gone.
    """
    from extensions.adapter_runner import (
        Strategy,
        adapter,
        clear_registry_for_tests,
    )
    from extensions.adapter_runner._ctx import AdapterContext
    from extensions.browser_control._dispatcher_bootstrap import reset_for_tests
    from extensions.browser_control._utils.errors import BrowserServiceError
    from extensions.browser_control.client.fetch import get_default_dispatcher_app

    reset_for_tests()
    clear_registry_for_tests()

    @adapter(
        site="smoke",
        name="probe",
        description="bug-A smoke probe",
        domain="example.invalid",
        strategy=Strategy.COOKIE,
        browser=True,
    )
    async def run(args, ctx):  # noqa: ARG001 — never invoked
        return []

    spec = run._adapter_spec  # type: ignore[attr-defined]

    ctx = AdapterContext.create(
        spec=spec,
        profile_home=Path("/tmp/oc-bug-a-smoke"),
    )

    bootstrap_error_seen = False
    other_error_seen = False
    try:
        await ctx.fetch_in_page("https://example.invalid/probe")
    except BrowserServiceError as exc:
        if "In-process dispatcher is not registered" in str(exc):
            bootstrap_error_seen = True
            print(f"  [fail] legacy bootstrap error surfaced: {exc}")
        else:
            other_error_seen = True
            print(f"  [ok] non-bootstrap BrowserServiceError (expected): "
                  f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        other_error_seen = True
        print(f"  [ok] non-bootstrap error (expected): "
              f"{type(exc).__name__}: {exc}")

    clear_registry_for_tests()

    if bootstrap_error_seen:
        return False
    if get_default_dispatcher_app() is None:
        print("  [fail] dispatcher slot empty after ctx.fetch_in_page")
        return False
    print(f"  [ok] dispatcher slot populated; other_error_seen={other_error_seen}")
    return True


async def main() -> int:
    print("Browser-port v0.5 Bug A smoke test")
    print("=" * 60)

    ok = True

    print("\n[1/2] fetch_browser_json direct path")
    try:
        if not await _smoke_fetch_browser_json_path():
            ok = False
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False

    print("\n[2/2] adapter ctx → fetch_in_page path")
    try:
        if not await _smoke_adapter_ctx_path():
            ok = False
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        ok = False

    print("\n" + "=" * 60)
    if ok:
        print("RESULT: PASS — Bug A regressions confirmed fixed")
        return 0
    print("RESULT: FAIL — see output above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
