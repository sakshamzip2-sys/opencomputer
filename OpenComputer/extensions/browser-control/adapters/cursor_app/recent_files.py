"""Adapter: cursor_app/recent_files — Cursor.app recently-opened files.

Strategy.INTERCEPT — Cursor is an Electron app exposing CDP on a
configurable debug port. v0.4 ships a thin starter; v0.5's deeper
Electron control layer (DEFERRED.md §C) extends with auto-detect +
multi-app helpers.

Pre-req: launch Cursor with ``--remote-debugging-port=19222`` (or the
port set in the ``CURSOR_CDP_PORT`` env var).
"""

from __future__ import annotations

import os

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="cursor_app",
    name="recent_files",
    description="Cursor.app — list recently-opened files (Electron CDP).",
    domain="cursor.local",
    strategy=Strategy.INTERCEPT,
    browser=True,
    args=[
        {"name": "limit", "type": "int", "default": 20, "help": "Max files"},
    ],
    columns=["path", "basename", "url"],
)
async def run(args, ctx):
    # Probe for the recent-files list in localStorage; Cursor (and
    # most Electron-based VS Code forks) cache the MRU list under a
    # well-known key. Real users will adjust the key per Cursor
    # version; this is the v1.x default.
    limit = max(1, int(args.get("limit") or 20))
    expr = f"""
        (() => {{
          try {{
            const raw = localStorage.getItem('history.recentlyOpenedPathsList')
              || localStorage.getItem('recentlyOpened')
              || localStorage.getItem('mru');
            if (!raw) return [];
            const data = JSON.parse(raw);
            const list = Array.isArray(data) ? data
                       : Array.isArray(data.entries) ? data.entries
                       : [];
            return list.slice(0, {limit}).map(e => ({{
              path: typeof e === 'string' ? e : (e.fileUri || e.path || e.uri || ''),
            }}));
          }} catch (e) {{ return []; }}
        }})()
    """
    info = await ctx.evaluate(expr)
    if not isinstance(info, list):
        return []
    rows: list[dict] = []
    for item in info:
        path = item.get("path") if isinstance(item, dict) else ""
        if not isinstance(path, str) or not path:
            continue
        basename = path.rstrip("/").split("/")[-1] or path
        url = path if path.startswith(("file://", "vscode://")) else f"file://{path}"
        rows.append({"path": path, "basename": basename, "url": url})
    return rows


# Hint for the agent: the env var pattern used to point at Cursor's
# CDP port. The runner doesn't read this directly; it's documented for
# the user to surface to ``OPENCOMPUTER_BROWSER_CONTROL_URL`` or a
# similar config.
_CDP_PORT_ENV = os.environ.get("CURSOR_CDP_PORT", "19222")
