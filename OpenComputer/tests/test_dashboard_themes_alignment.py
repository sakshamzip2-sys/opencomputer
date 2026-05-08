"""Regression-lock the /api/v1/dashboard/themes server list against the JS dict.

Prior to 2026-05-08, ``routes/dashboard_meta.py:17`` returned
``["dark", "midnight", "high-contrast"]`` — themes that do NOT exist in
``static/_themes.js``. The actual JS dict exposed
``dark / light / solarized / monokai``. A ``PUT
/api/v1/dashboard/theme {"name": "light"}`` therefore returned 400 even
though the client-side picker offered "light" as a choice.

This test parses the JS source as source-of-truth and asserts the
server's ``_THEMES`` list matches. See:
``docs/superpowers/specs/2026-05-08-hermes-dashboard-ext-rl-providers-parity-design.md``
§2.2.4 for the bug history.
"""

from __future__ import annotations

import re
from pathlib import Path

from opencomputer.dashboard.routes.dashboard_meta import _THEMES


def _extract_themes_from_js(js_text: str) -> set[str]:
    """Parse the THEMES object literal from _themes.js.

    Walks the body of ``const THEMES = { ... }`` tracking brace depth.
    Keys at depth 0 (immediately inside THEMES) are theme names; keys
    at depth >= 1 are nested (vars, etc.) and ignored.
    """
    start = js_text.find("const THEMES = {")
    assert start != -1, (
        "static/_themes.js no longer declares `const THEMES = {`; "
        "update this test to match the new structure."
    )
    body = js_text[start + len("const THEMES = {"):]
    depth = 0
    keys: list[str] = []
    line_buf = ""
    key_re = re.compile(r"\b([a-z][a-z0-9_-]*)\s*:\s*$")
    for ch in body:
        if ch == "{":
            if depth == 0:
                m = key_re.search(line_buf)
                if m:
                    keys.append(m.group(1))
            depth += 1
            line_buf = ""
        elif ch == "}":
            depth -= 1
            if depth < 0:
                break
            line_buf = ""
        elif ch == "\n":
            line_buf = ""
        else:
            line_buf += ch
    return set(keys)


def test_dashboard_meta_themes_match_js_themes() -> None:
    """The /api/v1/dashboard/themes server list MUST match the JS dict."""
    js_path = (
        Path(__file__).resolve().parents[1]
        / "opencomputer"
        / "dashboard"
        / "static"
        / "_themes.js"
    )
    js_text = js_path.read_text(encoding="utf-8")
    js_themes = _extract_themes_from_js(js_text)
    assert js_themes, (
        "Failed to extract any themes from _themes.js — parser may be broken."
    )
    server_themes = set(_THEMES)
    assert js_themes == server_themes, (
        f"Server _THEMES {sorted(server_themes)} drifted from "
        f"static/_themes.js {sorted(js_themes)}; update "
        "routes/dashboard_meta.py:17 or static/_themes.js to match."
    )
