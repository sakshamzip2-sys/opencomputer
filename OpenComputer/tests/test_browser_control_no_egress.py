"""tests/test_browser_control_no_egress.py — local-only contract guard.

The legacy browser-control plugin source (top-level browser.py / plugin.py /
tools.py) MUST NOT directly import HTTP clients. Playwright handles networking
internally (browser ↔ websites); the legacy plugin layer above it has no
business reaching out to other endpoints.

The new browser-port subsystems under chrome/, session/, server/, client/,
_utils/, profiles/, snapshot/, tools_core/ are exempt: they implement an
explicit HTTP control plane (BLUEPRINT.md §4 mandates httpx + fastapi +
websockets + mcp). The deny-list below stays scoped to the legacy plugin
files that pre-date the port.

To DELIBERATELY add networking to a LEGACY file (a contract break):

1. Add the import.
2. Update ``_DENIED_NETWORK_IMPORTS`` below to remove the relevant entry.
3. Update ``extensions/browser-control/README.md`` privacy contract.
4. Update CHANGELOG.

Don't silently bypass this guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

_DENIED_NETWORK_IMPORTS: frozenset[str] = frozenset(
    {
        "httpx",
        "requests",
        "urllib3",
        "aiohttp",
        "websockets",
        "grpc",
        "boto3",
        "google.cloud",
    }
)

# Legacy plugin entry-point files. The new browser-port subpackages (under
# directory names listed in `_BROWSER_PORT_DIRS`) implement an explicit HTTP
# control plane and are exempt from this guard.
_LEGACY_FILES: tuple[str, ...] = ("browser.py", "plugin.py", "tools.py")
_BROWSER_PORT_DIRS: tuple[str, ...] = (
    "_utils",
    "profiles",
    "chrome",
    "session",
    "tools_core",
    "snapshot",
    "server",
    "server_context",
    "client",
    "providers",
)


def _plugin_root() -> Path:
    """Return the path to extensions/browser-control/ from this test file."""
    return Path(__file__).resolve().parent.parent / "extensions" / "browser-control"


def _legacy_python_files(root: Path) -> list[Path]:
    """Top-level legacy plugin files only — exempts the new browser-port subdirs."""
    return [root / name for name in _LEGACY_FILES if (root / name).is_file()]


def _scan(path: Path) -> list[tuple[int, str]]:
    """Return [(line_no, statement)] for any deny-listed import in the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DENIED_NETWORK_IMPORTS:
                    out.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _DENIED_NETWORK_IMPORTS:
                names = ", ".join(a.name for a in node.names)
                out.append((node.lineno, f"from {node.module} import {names}"))
    return out


def test_no_network_imports_in_browser_control_plugin():
    """Sweep the legacy entry-point files for HTTP-client imports."""
    violations: list[str] = []
    for path in _legacy_python_files(_plugin_root()):
        for line_no, stmt in _scan(path):
            violations.append(f"{path}:{line_no}: {stmt}")
    assert not violations, (
        "legacy browser-control plugin files must NOT import network libs:\n  "
        + "\n  ".join(violations)
        + "\n\nIf the network import is intentional: update the deny-list, "
        + "update extensions/browser-control/README.md privacy contract, "
        + "and document in CHANGELOG."
    )


def test_no_urllib_request():
    """``urllib.request`` is stdlib so the import-name check above won't
    catch it — explicit AST sweep over legacy files."""
    violations: list[str] = []
    for path in _legacy_python_files(_plugin_root()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                violations.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "urllib.request":
                        violations.append(f"{path}:{node.lineno}")
    assert not violations, "urllib.request leaked: " + str(violations)


def test_no_socket_module():
    """Direct ``socket`` usage is also forbidden in legacy files (low-level network)."""
    violations: list[str] = []
    for path in _legacy_python_files(_plugin_root()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "socket":
                        violations.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module == "socket":
                violations.append(f"{path}:{node.lineno}")
    assert not violations, "socket module used: " + str(violations)


def test_plugin_root_exists():
    """Sanity: confirm the test is actually scanning a real directory.

    Without this, an empty plugin dir would silently pass.
    """
    root = _plugin_root()
    assert root.exists(), f"plugin root not found at {root}"
    legacy = _legacy_python_files(root)
    assert legacy, f"expected legacy entry-point files in {root}, found none"
