"""tests/test_skill_evolution_no_egress.py — local-only contract guard.

The skill-evolution plugin's daemon code MUST NOT directly import HTTP
clients. LLM calls go via existing provider plugins (which use httpx
internally), so the plugin SOURCE itself should have ZERO httpx /
requests / aiohttp imports.

(Provider plugins are exempt — that's where networking belongs.)

To DELIBERATELY add networking to this plugin (a contract break):

1. Add the import.
2. Update ``_DENIED_NETWORK_IMPORTS`` below to remove the relevant entry.
3. Update the README's privacy contract section.
4. Update CHANGELOG.

Don't silently bypass this guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Importable names of HTTP/network libraries the skill-evolution plugin
# must not use directly. Provider plugins are the sanctioned channel.
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


def _plugin_root() -> Path:
    """Return the path to extensions/skill-evolution/ from this test file."""
    return Path(__file__).resolve().parent.parent / "extensions" / "skill-evolution"


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


def test_no_network_imports_in_skill_evolution_plugin():
    """Sweep extensions/skill-evolution/*.py for HTTP-client imports."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
        for line_no, stmt in _scan(path):
            violations.append(f"{path}:{line_no}: {stmt}")
    assert not violations, (
        "skill-evolution must NOT import network libs:\n  "
        + "\n  ".join(violations)
        + "\n\nIf the network import is intentional: update the deny-list, "
        + "update extensions/skill-evolution/README.md privacy contract, "
        + "and document in CHANGELOG."
    )


def test_no_urllib_request():
    """``urllib.request`` is stdlib so the import-name check above won't
    catch it — explicit AST sweep."""
    violations = []
    for path in _plugin_root().rglob("*.py"):
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
    """Direct ``socket`` usage is also forbidden (low-level network access)."""
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
    py_files = list(root.rglob("*.py"))
    assert len(py_files) >= 5, f"expected 5+ .py files, found {len(py_files)}"
