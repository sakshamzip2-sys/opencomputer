"""Adapter-runner client — lets ``adapter-runner`` route browser ops via opencli.

Mirror of ``extensions.browser_harness.actions.BrowserHarnessActions`` so
adapter-runner's ``_resolve_browser_actions()`` can swap between backends
based on ``OPENCOMPUTER_BROWSER_BACKEND``. The shape (method names,
return-dict keys) matches the legacy ``BrowserActions`` contract that
adapter-runner already calls.

Most adapter-runner code paths use ``browser_navigate`` + ``browser_act``
(``kind: "evaluate" | "click"``). We map those onto opencli browser
sub-commands. Anything more exotic (typing into specific fields, complex
multi-step flows) goes through ``browser_act`` with a ``kind`` we don't
recognize → returns a structured error so the adapter can decide.
"""

from __future__ import annotations

import logging
from typing import Any

import opencli_dispatcher as dispatcher  # type: ignore[import-not-found]

_log = logging.getLogger("opencomputer.opencli_bridge.actions")


class OpenCliBridgeActions:
    """Drop-in for ``BrowserActions`` that talks to opencli's browser bridge."""

    def __init__(self, *, profile: str | None = None) -> None:
        self.profile = profile

    def browser_navigate(
        self,
        url: str,
        *,
        target_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """``opencli browser open <url>`` — returns dict with target_id, url, title."""
        prof = profile or self.profile
        result = dispatcher.run_browser(
            "open",
            args=[url],
            profile=prof,
            target=target_id,
        )
        # Normalize to the shape adapter-runner expects.
        out: dict[str, Any] = {
            "url": result.get("url") or url,
            "title": result.get("title"),
            "targetId": result.get("targetId") or result.get("target_id") or target_id,
            "target_id": result.get("target_id") or result.get("targetId") or target_id,
            "raw": result,
        }
        if "error" in result:
            out["error"] = result["error"]
        return out

    def browser_act(
        self,
        action: dict[str, Any],
        *,
        profile: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        """Map a typed action dict onto an opencli browser sub-command.

        Recognized ``kind`` values:
          * ``evaluate`` → ``opencli browser eval <expression>``
          * ``click`` → ``opencli browser click <selector|@ref>``
          * ``type`` → ``opencli browser type <selector> <text>``
          * ``fill`` → ``opencli browser fill <selector> <text>``
          * ``extract`` → ``opencli browser extract <selector>``
          * ``state`` → ``opencli browser state``
          * ``screenshot`` → ``opencli browser screenshot``
          * ``wait`` → ``opencli browser wait <selector|ms>``

        Anything else → structured error so the adapter can branch.
        """
        kind = action.get("kind")
        prof = profile or self.profile

        if kind == "evaluate":
            expr = action.get("expression") or ""
            return dispatcher.run_browser(
                "eval", args=[expr], profile=prof, target=target_id
            )
        if kind == "click":
            sel = action.get("selector") or action.get("ref") or ""
            return dispatcher.run_browser(
                "click", args=[sel], profile=prof, target=target_id
            )
        if kind == "type":
            sel = action.get("selector") or ""
            text = action.get("text") or ""
            return dispatcher.run_browser(
                "type", args=[sel, text], profile=prof, target=target_id
            )
        if kind == "fill":
            sel = action.get("selector") or ""
            text = action.get("text") or ""
            return dispatcher.run_browser(
                "fill", args=[sel, text], profile=prof, target=target_id
            )
        if kind == "extract":
            sel = action.get("selector") or ""
            return dispatcher.run_browser(
                "extract", args=[sel], profile=prof, target=target_id
            )
        if kind == "state":
            return dispatcher.run_browser(
                "state", profile=prof, target=target_id
            )
        if kind == "screenshot":
            return dispatcher.run_browser(
                "screenshot", profile=prof, target=target_id
            )
        if kind == "wait":
            arg = action.get("selector") or action.get("ms") or ""
            return dispatcher.run_browser(
                "wait", args=[str(arg)], profile=prof, target=target_id
            )

        return {
            "error": "unsupported_kind",
            "kind": kind,
            "hint": (
                "OpenCliBridgeActions supports evaluate/click/type/fill/extract/"
                "state/screenshot/wait. Use OpenCliBrowse directly for richer "
                "actions."
            ),
        }

    def close(self) -> None:
        """Best-effort tab close. Daemon stays up — atexit hook from "
        "browser-harness owns Chrome lifecycle."""
        try:
            dispatcher.run_browser("close", profile=self.profile)
        except Exception as exc:  # noqa: BLE001
            _log.debug("OpenCliBridgeActions.close failed (non-fatal): %s", exc)


__all__ = ["OpenCliBridgeActions"]
