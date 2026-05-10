"""BrowserHarnessActions — adapter-runner-compatible client over the lifted Hermes dispatcher.

OC's adapter-runner (see ``extensions/adapter-runner/_ctx.py``) talks to a
browser through a ``BrowserActions`` client object. The original
implementation lived in ``extensions/browser-control/client/`` and routed
each call through OC's broken Playwright/CDP path. This module provides a
drop-in replacement that routes the same call shape through the
``agent-browser`` CLI via the lifted Hermes dispatcher.

Adapter-runner's call shape (from ``_ctx.py``):

  await actions.browser_navigate(url=..., target_id=..., profile=...)
  await actions.browser_act({"kind": "evaluate", "expression": js}, profile=...)
  await actions.browser_act({"kind": "click", "ref": ref}, profile=...)

Mapping to Hermes-dispatcher concepts:

  - ``target_id`` (Chrome CDP target id, hex) → ``task_id`` (Hermes
    session-isolation key, opaque string). Adapter-runner only uses
    ``target_id`` to keep subsequent calls on the same tab; the value
    itself is opaque to it. We accept whatever it passes and use it as
    a Hermes ``task_id``. Falls back to ``"default"`` when unset.
  - ``browser_act({"kind": "evaluate", ...})`` → ``dispatcher.browser_console(expression=js)``
    which routes to ``agent-browser eval`` and returns ``{success, result}``.
  - ``browser_act({"kind": "click", "ref": ref})`` → ``dispatcher.browser_click(ref)``.

The dispatcher is synchronous (subprocess.Popen + wait); each call is
parked on the default thread executor via ``asyncio.to_thread`` so the
adapter-runner's event loop isn't blocked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Sibling-module import — loader puts the plugin dir on sys.path,
# OR adapter-runner's _ctx.py adds it manually before this import fires.
import dispatcher as _bt  # type: ignore[import-not-found]


class BrowserHarnessActions:
    """Drop-in for ``extensions.browser_control.client.BrowserActions``.

    Exposes the ``browser_navigate`` and ``browser_act`` methods that
    adapter-runner's ``AdapterContext`` calls. All other ``BrowserActions``
    methods are NOT used by adapter-runner today and are intentionally
    omitted; if a future adapter needs more, add them here.
    """

    async def browser_navigate(
        self,
        *,
        url: str,
        target_id: str | None = None,
        profile: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Navigate the browser to ``url`` in the session keyed by ``target_id``.

        Adapter-runner uses the returned dict's ``targetId`` / ``target_id``
        to remember the session for subsequent calls. We synthesise it from
        whatever ``task_id`` we used so adapter-runner threads it back to us
        on follow-up calls.
        """
        task_id = (target_id or profile or "default").strip() or "default"
        result_json = await asyncio.to_thread(
            _bt.browser_navigate, url, task_id=task_id
        )
        try:
            result = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result = {"success": False, "error": "invalid dispatcher response"}

        nav_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            "ok": bool(result.get("success")),
            "targetId": task_id,
            "target_id": task_id,
            "url": (nav_data.get("url") if isinstance(nav_data.get("url"), str) else None) or url,
            "title": nav_data.get("title"),
            "snapshot": result.get("snapshot"),
            **({"error": result["error"]} if not result.get("success") and result.get("error") else {}),
        }

    async def browser_act(
        self,
        action: dict[str, Any],
        *,
        profile: str | None = None,
        target_id: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Dispatch one of: ``evaluate``, ``click``.

        Adapter-runner only uses these two ``kind`` values today. Returns
        a dict with ``result`` / ``value`` / ``data`` populated as
        applicable so ``_ctx.py``'s ``for key in ('result', 'value', 'data'):``
        unpacker finds something useful.
        """
        kind = action.get("kind")
        task_id = (target_id or profile or "default").strip() or "default"

        if kind == "evaluate":
            expression = action.get("expression")
            if not isinstance(expression, str):
                return {"result": None, "error": "evaluate requires 'expression' (str)"}
            result_json = await asyncio.to_thread(
                _bt.browser_console, expression=expression, task_id=task_id,
            )
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                return {"result": None, "error": "invalid dispatcher response"}
            # _browser_eval returns {"success": True, "result": <parsed>, ...}
            # or {"success": False, "error": ...}
            if not result.get("success"):
                return {"result": None, "error": result.get("error", "evaluate failed")}
            return {"result": result.get("result")}

        if kind == "click":
            ref = action.get("ref")
            if not isinstance(ref, str):
                return {"ok": False, "error": "click requires 'ref' (str)"}
            result_json = await asyncio.to_thread(
                _bt.browser_click, ref, task_id=task_id,
            )
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                return {"ok": False, "error": "invalid dispatcher response"}
            return {
                "ok": bool(result.get("success")),
                **({"error": result["error"]} if not result.get("success") and result.get("error") else {}),
            }

        return {"ok": False, "error": f"BrowserHarnessActions: unsupported act kind {kind!r}"}


__all__ = ["BrowserHarnessActions"]
