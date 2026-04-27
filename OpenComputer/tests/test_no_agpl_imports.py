"""tests/test_no_agpl_imports.py — forward-looking AGPL contagion guard.

OpenComputer is MIT-licensed. This test scans the source tree for imports of
known-AGPL packages so a contributor never silently re-introduces an AGPL
dep. When a new AGPL dep is desired (e.g. for an isolated subprocess shim),
the policy is: explicitly add it to a documented isolation boundary AND
remove it from this deny-list with a CHANGELOG note.

Updated 2026-04-27 after the Open Interpreter (AGPL) removal — see
``docs/superpowers/specs/2026-04-27-oi-removal-native-introspection-design.md``.

This replaces the OI-specific regex guard previously in
``tests/test_coding_harness_oi_agpl_boundary.py`` (deleted in T8). The new
test uses AST parsing (so commented-out imports don't trigger and string
literals can't fake matches) and is parameterizable via the deny-list.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Importable names of AGPL packages OpenComputer must NOT depend on.
# Each name matches the leaf name of `import X` or `from X import Y`,
# AND the root of dotted names (so `interpreter.foo` matches `interpreter`).
_AGPL_DENY_LIST: frozenset[str] = frozenset({
    "interpreter",  # Open Interpreter (AGPL-3.0). Removed 2026-04-27.
})

# Source roots to scan. Tests, docs, build artifacts excluded.
_SCAN_ROOTS: tuple[str, ...] = ("opencomputer", "plugin_sdk", "extensions")

# Directories to prune during walk.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def _project_root() -> Path:
    """Return the OpenComputer/ directory (the dir holding pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not locate OpenComputer pyproject.toml from " + str(here))


def _iter_source_files() -> list[Path]:
    root = _project_root()
    files: list[Path] = []
    for scan_dir_name in _SCAN_ROOTS:
        scan_dir = root / scan_dir_name
        if not scan_dir.exists():
            continue
        for p in scan_dir.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            files.append(p)
    return files


def _violations_in_file(path: Path) -> list[tuple[int, str]]:
    """Return [(line_no, source_line)] for any AGPL imports found."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []  # malformed file — not our concern here

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".")[0]
                if root_name in _AGPL_DENY_LIST:
                    out.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root_name = module.split(".")[0]
            if root_name in _AGPL_DENY_LIST:
                names = ", ".join(a.name for a in node.names)
                out.append((node.lineno, f"from {module} import {names}"))
    return out


def test_no_agpl_imports_in_source_tree():
    """Source code under opencomputer/, plugin_sdk/, extensions/ must not
    import any package on the AGPL deny-list."""
    failures: list[str] = []
    for path in _iter_source_files():
        for line_no, stmt in _violations_in_file(path):
            failures.append(f"{path}:{line_no}: {stmt}")

    assert not failures, (
        "AGPL imports detected in source tree (deny-list: "
        + ", ".join(sorted(_AGPL_DENY_LIST))
        + "):\n  "
        + "\n  ".join(failures)
        + "\n\nIf this dep is intentional, isolate it behind a subprocess + "
        + "remove it from _AGPL_DENY_LIST with a CHANGELOG note explaining the "
        + "license-isolation strategy."
    )


def test_deny_list_root_only_matches_root_imports():
    """Sanity: matching is on the root of dotted names, not a substring.

    Importing ``interpreter_helpers`` (a hypothetical MIT package whose name
    happens to start with 'interpreter') must NOT match the 'interpreter'
    deny-list entry.
    """
    fake_src = "import interpreter_helpers\nfrom interpreter_helpers import foo\n"
    tree = ast.parse(fake_src)
    matches: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _AGPL_DENY_LIST:
                    matches.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _AGPL_DENY_LIST:
                matches.append(module)
    assert matches == [], f"Substring-style matching leaked: {matches}"


def test_deny_list_catches_dotted_import():
    """A dotted import like ``import interpreter.computer`` MUST trigger."""
    fake_src = "import interpreter.computer\n"
    tree = ast.parse(fake_src)
    matches: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _AGPL_DENY_LIST:
                    matches.append(alias.name)
    assert matches == ["interpreter.computer"]


def test_deny_list_catches_from_import():
    """A ``from interpreter.X import Y`` statement MUST trigger."""
    fake_src = "from interpreter.core import OpenInterpreter\n"
    tree = ast.parse(fake_src)
    matches: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _AGPL_DENY_LIST:
                matches.append(module)
    assert matches == ["interpreter.core"]
