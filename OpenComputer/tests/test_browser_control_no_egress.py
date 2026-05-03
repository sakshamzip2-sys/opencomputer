"""tests/test_browser_control_no_egress.py — local-only contract guard.

After Wave 3 the legacy ``_browser_session.py`` / ``_tools.py`` /
``chrome_launch.py`` files are gone. The post-W3 plugin surface is:

  - ``plugin.py``      — entry; registers Browser + shims + doctor row
  - ``_tool.py``       — Browser tool + 11 deprecation shims
  - ``schema.py``      — pydantic schema for BrowserParams / ActRequest

These three files run in the host Python process and have no business
reaching out to arbitrary network hosts. Networking is handled by:

  - ``client/`` — talks to loopback (in-process dispatcher OR loopback
    HTTP) only. ``client/auth.py:is_loopback_url`` enforces this and
    is exempt from the deny-list because httpx is the entire point.
  - ``server/`` + ``chrome/`` + ``session/`` + ``server_context/`` +
    ``snapshot/`` + ``tools_core/`` + ``profiles/`` + ``_utils/`` — the
    explicit HTTP control plane. ``BLUEPRINT.md §4`` mandates httpx +
    fastapi + websockets + mcp here. Exempt.

To deliberately add networking to a guarded entry-point file (a contract
break):

1. Add the import.
2. Update ``_DENIED_NETWORK_IMPORTS`` below.
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

# Top-level files whose business is the agent-facing tool surface, not
# the network. After W3 there are three: the plugin entry, the tool
# implementation, and the pydantic schema.
_GUARDED_FILES: tuple[str, ...] = ("plugin.py", "_tool.py", "schema.py")

# Subpackages that DO need networking and are exempt:
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


def _guarded_python_files(root: Path) -> list[Path]:
    """Top-level guarded files only — exempts the new browser-port subdirs."""
    return [root / name for name in _GUARDED_FILES if (root / name).is_file()]


def _scan(path: Path) -> list[tuple[int, str]]:
    """Return [(line_no, statement)] for any deny-listed MODULE-SCOPE
    import in the file.

    Function-local imports inside guarded files are tolerated: the
    doctor probe in ``plugin.py`` legitimately imports ``httpx``
    lazily, but only when the operator opts in via
    ``OPENCOMPUTER_BROWSER_CONTROL_URL``. The contract that matters is
    "no eager networking at import time" — module-scope is the right
    boundary.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out: list[tuple[int, str]] = []
    for node in tree.body:  # module-scope only — no recursion into defs
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
    """Sweep the agent-facing entry files for HTTP-client imports."""
    violations: list[str] = []
    for path in _guarded_python_files(_plugin_root()):
        for line_no, stmt in _scan(path):
            violations.append(f"{path}:{line_no}: {stmt}")
    # plugin.py legitimately imports httpx INSIDE _doctor_run for the
    # optional control-port reachability probe — but only when the
    # operator opts in via OPENCOMPUTER_BROWSER_CONTROL_URL. That import
    # is local to the function (lazy) so it's allowed; the AST scan
    # catches module-level imports.
    assert not violations, (
        "guarded browser-control entry files must NOT import network "
        "libs at module scope:\n  "
        + "\n  ".join(violations)
        + "\n\nIf the network import is intentional: update the "
        "deny-list, update extensions/browser-control/README.md privacy "
        "contract, and document in CHANGELOG."
    )


def test_no_urllib_request():
    """``urllib.request`` is stdlib so the import-name check above won't
    catch it — explicit module-scope sweep over guarded files."""
    violations: list[str] = []
    for path in _guarded_python_files(_plugin_root()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:  # module-scope only
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                violations.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "urllib.request":
                        violations.append(f"{path}:{node.lineno}")
    assert not violations, "urllib.request leaked: " + str(violations)


def test_no_socket_module():
    """Direct ``socket`` usage is also forbidden in guarded files (low-level network)."""
    violations: list[str] = []
    for path in _guarded_python_files(_plugin_root()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:  # module-scope only
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
    guarded = _guarded_python_files(root)
    assert guarded, (
        f"expected guarded entry files in {root}, found none. "
        f"Wave 3 should leave at least plugin.py, _tool.py, and schema.py."
    )


def test_client_subpackage_uses_loopback_only():
    """Spot-check: ``client/auth.py`` exposes ``is_loopback_host`` and
    ``client/fetch.py`` references it. The actual loopback enforcement
    is unit-tested elsewhere; here we just guard against the import
    surface drifting away from "loopback only".
    """
    client_dir = _plugin_root() / "client"
    assert client_dir.is_dir(), f"client/ subpackage missing at {client_dir}"
    auth_path = client_dir / "auth.py"
    fetch_path = client_dir / "fetch.py"
    assert auth_path.is_file()
    assert fetch_path.is_file()
    auth_src = auth_path.read_text(encoding="utf-8")
    fetch_src = fetch_path.read_text(encoding="utf-8")
    assert "def is_loopback_host" in auth_src
    assert "is_loopback_host" in fetch_src or "is_loopback_url" in fetch_src
