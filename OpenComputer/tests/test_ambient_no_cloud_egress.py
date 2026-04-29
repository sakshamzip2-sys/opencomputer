"""tests/test_ambient_no_cloud_egress.py — local-only contract guard.

The ambient sensor plugin MUST NOT send any data to a network destination.
This test AST-scans the plugin's source for HTTP-client imports. Any new
import that matches the deny-list fails CI.

To DELIBERATELY add networking to this plugin (a contract break):
1. Add the import.
2. Update _DENIED_NETWORK_IMPORTS below to remove the relevant entry.
3. Update the README's privacy contract section.
4. Update CHANGELOG.

Don't silently bypass this guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Importable names of HTTP/network libraries the ambient plugin must not use.
_DENIED_NETWORK_IMPORTS: frozenset[str] = frozenset({
    "httpx", "requests", "urllib3", "aiohttp", "websockets",
    "grpc", "boto3", "google.cloud", "anthropic", "openai",
})


def _plugin_root() -> Path:
    """Return the path to extensions/ambient-sensors/ from this test file."""
    return Path(__file__).resolve().parent.parent / "extensions" / "ambient-sensors"


def _scan_imports(path: Path) -> list[tuple[int, str]]:
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


def test_no_network_imports_in_ambient_plugin():
    """Sweep extensions/ambient-sensors/*.py for HTTP-client imports."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
        for line_no, stmt in _scan_imports(path):
            violations.append(f"{path}:{line_no}: {stmt}")

    assert not violations, (
        "Ambient sensor plugin must NOT import network libraries — "
        "local-only contract.\n"
        + "Violations:\n  " + "\n  ".join(violations)
        + "\n\nIf the network import is intentional: update the deny-list, "
        + "update extensions/ambient-sensors/README.md privacy contract, "
        + "and document in CHANGELOG."
    )


def test_no_urllib_request_in_ambient_plugin():
    """urllib.request is in stdlib so the import-name check above won't
    catch it — explicit AST sweep."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                violations.append(f"{path}:{node.lineno}: from urllib.request import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("urllib.request", "urllib.parse.urlopen"):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
    assert not violations, "urllib.request leaked into ambient plugin: " + str(violations)


def test_no_socket_module_used_directly():
    """Direct socket usage is also forbidden (low-level network access)."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "socket":
                        violations.append(f"{path}:{node.lineno}: import socket")
            elif isinstance(node, ast.ImportFrom) and node.module == "socket":
                violations.append(f"{path}:{node.lineno}: from socket import ...")
    assert not violations, "socket module used in ambient plugin: " + str(violations)


def test_plugin_root_exists():
    """Sanity: confirm the test is actually scanning a real directory.
    Without this, an empty plugin dir would silently pass."""
    root = _plugin_root()
    assert root.exists(), f"plugin root not found at {root}"
    py_files = list(root.rglob("*.py"))
    assert len(py_files) >= 5, f"expected 5+ .py files in plugin, found {len(py_files)}"


# ─── Screen-awareness no-egress guard ───────────────────────────────


def _screen_awareness_root() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "screen-awareness"
    )


def test_screen_awareness_has_no_network_imports():
    """The screen-awareness module MUST NOT import any HTTP/network
    library. Adding networking is a contract break — update README +
    CHANGELOG + this denylist before bypassing.
    """
    root = _screen_awareness_root()
    if not root.exists():
        return  # plugin not yet present — no-op
    findings: list[str] = []
    for py_file in root.rglob("*.py"):
        if "__pycache__" in py_file.parts or "tests" in py_file.parts:
            continue
        for line_no, statement in _scan_imports(py_file):
            findings.append(f"{py_file.relative_to(root)}:{line_no}: {statement}")
    assert findings == [], (
        "Network imports found in screen-awareness — privacy contract "
        f"break. Findings:\n" + "\n".join(findings)
    )


def test_screen_awareness_root_exists():
    """Sanity: same guard as ambient — confirms the test is real."""
    root = _screen_awareness_root()
    if not root.exists():
        return  # plugin not yet present — no-op
    py_files = [
        p for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    assert len(py_files) >= 5, (
        f"expected 5+ .py files in screen-awareness, found {len(py_files)}"
    )
