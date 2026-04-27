# Native Cross-Platform Introspection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OpenComputer's Open Interpreter subprocess bridge with a native pure-pip cross-platform introspection module, preserving the 5 tool names and F1 capability semantics, gaining Windows support, and removing AGPL exposure.

**Architecture:** New `extensions/coding-harness/introspection/` (3 files, ~300 LOC) provides 5 tools backed by `psutil`, `mss`, `pyperclip`, `rapidocr-onnxruntime`. Old `extensions/coding-harness/oi_bridge/` (~1,308 LOC) plus `extensions/oi-capability/` shim and 5 OI test files (~1,220 LOC) deleted. Plugin registration swapped atomically. F1 capability namespace migrates `oi_bridge.X` → `introspection.X`. Doctor adds two checks (orphan venv + dep verification).

**Tech Stack:** Python 3.12+; `psutil>=5.9`, `mss>=9.0`, `pyperclip>=1.8`, `rapidocr-onnxruntime>=1.4`. Existing `plugin_sdk.consent.CapabilityClaim`, `plugin_sdk.tool_contract.BaseTool`, `plugin_sdk.core.{ToolCall, ToolResult}` reused.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-04-27-oi-removal-native-introspection-design.md`

**Branch:** `feat/native-cross-platform-introspection` (worktree at `/tmp/oc-oi-removal/`).

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `extensions/coding-harness/introspection/__init__.py` | Package marker; re-export `ALL_TOOLS` | CREATE |
| `extensions/coding-harness/introspection/tools.py` | 5 BaseTool subclasses | CREATE |
| `extensions/coding-harness/introspection/ocr.py` | OCR helper (rapidocr-onnxruntime) | CREATE |
| `tests/test_introspection_list_app_usage.py` | psutil-backed test | CREATE |
| `tests/test_introspection_read_clipboard.py` | pyperclip-backed test | CREATE |
| `tests/test_introspection_screenshot.py` | mss-backed test | CREATE |
| `tests/test_introspection_list_recent_files.py` | pathlib-backed test | CREATE |
| `tests/test_introspection_extract_screen_text.py` | OCR test (mocked) | CREATE |
| `tests/test_introspection_capability_claims.py` | F1 namespace test | CREATE |
| `OpenComputer/pyproject.toml` | Add 4 deps | EDIT |
| `extensions/coding-harness/plugin.py` | Swap registration block | EDIT |
| `tests/conftest.py` | Remove OI alias plumbing | EDIT |
| `tests/test_sub_f1_license_boundary.py` | Drop OI-specific assertions | EDIT |
| `tests/test_tool_descriptions_audit.py` | Update path reference | EDIT |
| `opencomputer/security/sanitize.py` | Update docstring example | EDIT |
| `opencomputer/mcp/server.py` | Update docstring example | EDIT |
| `extensions/coding-harness/tools/point_click.py` | Update docstring | EDIT |
| `opencomputer/doctor.py` | Add 2 new checks | EDIT |
| `OpenComputer/CLAUDE.md` | Refresh §4 phase table | EDIT |
| `OpenComputer/CHANGELOG.md` | Unreleased entry | EDIT |
| `extensions/coding-harness/oi_bridge/` | Whole directory | DELETE |
| `extensions/oi-capability/` | Deprecated shim | DELETE |
| `tests/test_coding_harness_oi_subprocess_wrapper.py` | OI wrapper tests | DELETE |
| `tests/test_coding_harness_oi_venv_bootstrap.py` | OI venv tests | DELETE |
| `tests/test_coding_harness_oi_protocol.py` | OI protocol tests | DELETE |
| `tests/test_coding_harness_oi_telemetry_disable.py` | OI telemetry tests | DELETE |
| `tests/test_coding_harness_oi_agpl_boundary.py` | AGPL grep CI guard | DELETE |
| `tests/test_coding_harness_oi_tools_tier_1_introspection.py` | Old tier_1 tests | DELETE |
| `OpenComputer/docs/f7/` | F7/OI design docs (whole dir) | DELETE |

---

## Tasks

### Task 1: Add deps + create module skeleton

**Files:**
- Edit: `OpenComputer/pyproject.toml` (`[project.dependencies]` block)
- Create: `extensions/coding-harness/introspection/__init__.py`
- Create: `extensions/coding-harness/introspection/tools.py` (skeleton)
- Create: `extensions/coding-harness/introspection/ocr.py` (skeleton)

- [ ] **Step 1: Read current pyproject.toml deps**

Read `OpenComputer/pyproject.toml`, locate `[project.dependencies]` section.

- [ ] **Step 2: Add 4 new deps**

Append to `[project.dependencies]`:

```toml
psutil>=5.9
mss>=9.0
pyperclip>=1.8
rapidocr-onnxruntime>=1.4
```

- [ ] **Step 3: Create `__init__.py` with empty ALL_TOOLS export**

```python
"""Native cross-platform introspection tools.

Replaces the deprecated oi_bridge subprocess wrapper.
"""

from __future__ import annotations

from extensions.coding_harness.introspection.tools import (
    ExtractScreenTextTool,
    ListAppUsageTool,
    ListRecentFilesTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
)

ALL_TOOLS = [
    ListAppUsageTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
    ExtractScreenTextTool,
    ListRecentFilesTool,
]

__all__ = [
    "ALL_TOOLS",
    "ListAppUsageTool",
    "ReadClipboardOnceTool",
    "ScreenshotTool",
    "ExtractScreenTextTool",
    "ListRecentFilesTool",
]
```

- [ ] **Step 4: Create `tools.py` skeleton with all 5 classes (NotImplementedError bodies)**

Pre-populate the file with:
- Module docstring explaining cross-platform, no AGPL
- 5 class definitions, each with `consent_tier`, `parallel_safe`, `capability_claims`, `__init__(*, consent_gate=None, sandbox=None, audit=None)`, `schema` property, and `async def execute(self, call) -> ToolResult: raise NotImplementedError("Implementation lands in T2..T6")`.
- All 5 capability_claims use new namespace `introspection.X`.

- [ ] **Step 5: Create `ocr.py` skeleton**

```python
"""OCR backend for ExtractScreenTextTool.

Default: rapidocr-onnxruntime (cross-platform, ships ONNX model).
Future: optional macOS pyobjc Vision framework path.
"""

from __future__ import annotations


def ocr_text_from_screen() -> str:
    """Capture screen + OCR, return joined text."""
    raise NotImplementedError("Implementation lands in T6")
```

- [ ] **Step 6: Verify imports parse**

Run: `cd /tmp/oc-oi-removal && .venv/bin/python -c "from extensions.coding_harness.introspection import ALL_TOOLS; print(len(ALL_TOOLS))"`
Expected: prints `5` (after .venv has new deps installed via `pip install -e .`).

If import fails because rapidocr-onnxruntime is missing, that's expected — defer the import inside `ocr.py` to be lazy.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/pyproject.toml extensions/coding-harness/introspection/
git commit -m "feat(introspection): module skeleton + 4 native deps"
```

---

### Task 2: Implement ListAppUsageTool (psutil) — TDD

**Files:**
- Create: `tests/test_introspection_list_app_usage.py`
- Edit: `extensions/coding-harness/introspection/tools.py` (replace ListAppUsageTool body)

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_introspection_list_app_usage.py"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from extensions.coding_harness.introspection.tools import ListAppUsageTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_top_processes_by_cpu():
    fake_procs = [
        MagicMock(info={"name": "Chrome", "cpu_percent": 12.5, "create_time": 1714000000.0}),
        MagicMock(info={"name": "VSCode", "cpu_percent": 8.1, "create_time": 1714000100.0}),
        MagicMock(info={"name": "kernel_task", "cpu_percent": 1.2, "create_time": 1714000050.0}),
    ]

    with patch("extensions.coding_harness.introspection.tools.psutil.process_iter", return_value=fake_procs):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t1", name="list_app_usage", arguments={}))

    assert not result.is_error
    payload = json.loads(result.content)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "Chrome"
    assert payload[0]["cpu_percent"] == 12.5
    assert "started" in payload[0]
    assert len(payload) == 3


@pytest.mark.asyncio
async def test_capability_claim_namespace_is_introspection():
    claims = ListAppUsageTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "introspection.list_app_usage"


@pytest.mark.asyncio
async def test_handles_psutil_exception():
    with patch("extensions.coding_harness.introspection.tools.psutil.process_iter", side_effect=RuntimeError("psutil unavailable")):
        tool = ListAppUsageTool()
        result = await tool.execute(ToolCall(id="t2", name="list_app_usage", arguments={}))

    assert result.is_error
    assert "psutil unavailable" in result.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_introspection_list_app_usage.py -v`
Expected: FAIL with `NotImplementedError` from execute().

- [ ] **Step 3: Implement ListAppUsageTool.execute() in tools.py**

Add at module top: `import json, psutil, time`

Replace `ListAppUsageTool.execute`:

```python
async def execute(self, call: ToolCall) -> ToolResult:
    hours = int(call.arguments.get("hours", 8))
    cutoff = time.time() - hours * 3600

    try:
        rows = []
        for p in psutil.process_iter(["name", "cpu_percent", "create_time"]):
            info = p.info
            create_time = info.get("create_time") or 0.0
            if create_time < cutoff:
                continue
            rows.append(
                {
                    "name": info.get("name") or "<unknown>",
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                    "started": create_time,
                }
            )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

    rows.sort(key=lambda r: r["cpu_percent"], reverse=True)
    return ToolResult(tool_call_id=call.id, content=json.dumps(rows[:30]))
```

Update schema description: drop "Windows not supported" — replace with "Cross-platform (macOS, Linux, Windows). Returns JSON array of {name, cpu_percent, started} sorted by CPU usage."

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_introspection_list_app_usage.py -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add tests/test_introspection_list_app_usage.py extensions/coding-harness/introspection/tools.py
git commit -m "feat(introspection): ListAppUsageTool via psutil (cross-platform)"
```

---

### Task 3: Implement ReadClipboardOnceTool (pyperclip) — TDD

**Files:**
- Create: `tests/test_introspection_read_clipboard.py`
- Edit: `extensions/coding-harness/introspection/tools.py` (replace ReadClipboardOnceTool body)

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_introspection_read_clipboard.py"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from extensions.coding_harness.introspection.tools import ReadClipboardOnceTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_clipboard_text():
    with patch("extensions.coding_harness.introspection.tools.pyperclip.paste", return_value="hello"):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t1", name="read_clipboard_once", arguments={}))

    assert not result.is_error
    assert result.content == "hello"


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ReadClipboardOnceTool.capability_claims
    assert claims[0].capability_id == "introspection.read_clipboard_once"


@pytest.mark.asyncio
async def test_handles_pyperclip_error():
    with patch(
        "extensions.coding_harness.introspection.tools.pyperclip.paste",
        side_effect=Exception("xclip not installed"),
    ):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t2", name="read_clipboard_once", arguments={}))

    assert result.is_error
    assert "xclip" in result.content
```

- [ ] **Step 2: Run test, expect fail**

Run: `.venv/bin/pytest tests/test_introspection_read_clipboard.py -v` → FAIL.

- [ ] **Step 3: Implement**

Add at module top: `import pyperclip`

```python
async def execute(self, call: ToolCall) -> ToolResult:
    try:
        text = pyperclip.paste()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
    return ToolResult(tool_call_id=call.id, content=text)
```

Update schema description: keep "Platform: all" — pyperclip handles all 3.

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_introspection_read_clipboard.py -v` → 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add tests/test_introspection_read_clipboard.py extensions/coding-harness/introspection/tools.py
git commit -m "feat(introspection): ReadClipboardOnceTool via pyperclip (cross-platform)"
```

---

### Task 4: Implement ScreenshotTool (mss) — TDD

**Files:**
- Create: `tests/test_introspection_screenshot.py`
- Edit: `extensions/coding-harness/introspection/tools.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_introspection_screenshot.py"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from extensions.coding_harness.introspection.tools import ScreenshotTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_full_screen_returns_base64_png():
    fake_grab = MagicMock(rgb=b"\x00" * 12, size=(2, 2))
    fake_sct = MagicMock()
    fake_sct.__enter__.return_value = fake_sct
    fake_sct.__exit__.return_value = False
    fake_sct.monitors = [None, {"left": 0, "top": 0, "width": 100, "height": 100}]
    fake_sct.grab.return_value = fake_grab

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=fake_sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNGDATA"):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert not result.is_error
    decoded = base64.b64decode(result.content)
    assert decoded == b"PNGDATA"


@pytest.mark.asyncio
async def test_quadrant_top_left_uses_partial_bounds():
    fake_grab = MagicMock(rgb=b"\x00" * 12, size=(50, 50))
    fake_sct = MagicMock()
    fake_sct.__enter__.return_value = fake_sct
    fake_sct.__exit__.return_value = False
    fake_sct.monitors = [None, {"left": 0, "top": 0, "width": 100, "height": 100}]
    fake_sct.grab.return_value = fake_grab

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=fake_sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={"quadrant": "top-left"}))

    args = fake_sct.grab.call_args[0][0]
    assert args == {"left": 0, "top": 0, "width": 50, "height": 50}


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ScreenshotTool.capability_claims
    assert claims[0].capability_id == "introspection.screenshot"
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_introspection_screenshot.py -v` → FAIL.

- [ ] **Step 3: Implement**

Add at module top: `import base64; import mss; import mss.tools`

```python
def _quadrant_bounds(monitor: dict, quadrant: str) -> dict:
    half_w = monitor["width"] // 2
    half_h = monitor["height"] // 2
    if quadrant == "top-left":
        return {"left": monitor["left"], "top": monitor["top"], "width": half_w, "height": half_h}
    if quadrant == "top-right":
        return {"left": monitor["left"] + half_w, "top": monitor["top"], "width": half_w, "height": half_h}
    if quadrant == "bottom-left":
        return {"left": monitor["left"], "top": monitor["top"] + half_h, "width": half_w, "height": half_h}
    if quadrant == "bottom-right":
        return {"left": monitor["left"] + half_w, "top": monitor["top"] + half_h, "width": half_w, "height": half_h}
    return monitor


# inside ScreenshotTool:
async def execute(self, call: ToolCall) -> ToolResult:
    quadrant = call.arguments.get("quadrant")
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            if quadrant:
                monitor = _quadrant_bounds(monitor, quadrant)
            shot = sct.grab(monitor)
            png = mss.tools.to_png(shot.rgb, shot.size)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
    return ToolResult(tool_call_id=call.id, content=base64.b64encode(png).decode("ascii"))
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_introspection_screenshot.py -v` → 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add tests/test_introspection_screenshot.py extensions/coding-harness/introspection/tools.py
git commit -m "feat(introspection): ScreenshotTool via mss (cross-platform)"
```

---

### Task 5: Implement ListRecentFilesTool (pathlib) — TDD

**Files:**
- Create: `tests/test_introspection_list_recent_files.py`
- Edit: `extensions/coding-harness/introspection/tools.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_introspection_list_recent_files.py"""
from __future__ import annotations

import json
import os
import time

import pytest

from extensions.coding_harness.introspection.tools import ListRecentFilesTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_files_modified_within_window(tmp_path):
    recent = tmp_path / "recent.txt"
    recent.write_text("recent")

    old = tmp_path / "old.txt"
    old.write_text("old")
    old_mtime = time.time() - 24 * 3600
    os.utime(old, (old_mtime, old_mtime))

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 10},
    ))

    assert not result.is_error
    payload = json.loads(result.content)
    paths = [r["path"] for r in payload]
    assert any("recent.txt" in p for p in paths)
    assert all("old.txt" not in p for p in paths)


@pytest.mark.asyncio
async def test_skips_dunder_dirs(tmp_path):
    pyc_dir = tmp_path / "__pycache__"
    pyc_dir.mkdir()
    (pyc_dir / "junk.pyc").write_text("compiled")

    real = tmp_path / "real.py"
    real.write_text("source")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 10},
    ))

    payload = json.loads(result.content)
    assert all("__pycache__" not in r["path"] for r in payload)


@pytest.mark.asyncio
async def test_limit_caps_results(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")

    tool = ListRecentFilesTool()
    result = await tool.execute(ToolCall(
        id="t1", name="list_recent_files",
        arguments={"hours": 1, "directory": str(tmp_path), "limit": 5},
    ))

    payload = json.loads(result.content)
    assert len(payload) == 5


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ListRecentFilesTool.capability_claims
    assert claims[0].capability_id == "introspection.list_recent_files"
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_introspection_list_recent_files.py -v` → FAIL.

- [ ] **Step 3: Implement**

Add at module top (if not present): `import os, time, json` (already from T2); plus `from pathlib import Path`.

```python
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache"})


def _walk_recent_files(base: Path, cutoff: float, limit: int) -> list[tuple[float, Path]]:
    out: list[tuple[float, Path]] = []
    cap = limit * 2
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            p = Path(root) / fname
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > cutoff:
                out.append((m, p))
                if len(out) >= cap:
                    return out
    return out


# inside ListRecentFilesTool:
async def execute(self, call: ToolCall) -> ToolResult:
    hours = int(call.arguments.get("hours", 8))
    directory = call.arguments.get("directory", "~")
    limit = int(call.arguments.get("limit", 50))

    base = Path(os.path.expanduser(directory))
    if not base.exists() or not base.is_dir():
        return ToolResult(tool_call_id=call.id, content=f"Error: directory not found: {directory}", is_error=True)

    cutoff = time.time() - hours * 3600

    try:
        rows = _walk_recent_files(base, cutoff, limit)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

    rows.sort(reverse=True)
    payload = [{"path": str(p), "mtime": m} for m, p in rows[:limit]]
    return ToolResult(tool_call_id=call.id, content=json.dumps(payload))
```

Schema: drop "Platform: macOS, Linux" — replace with "Cross-platform. Returns JSON of {path, mtime} sorted newest-first."

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_introspection_list_recent_files.py -v` → 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add tests/test_introspection_list_recent_files.py extensions/coding-harness/introspection/tools.py
git commit -m "feat(introspection): ListRecentFilesTool via pathlib (cross-platform)"
```

---

### Task 6: Implement ExtractScreenTextTool (rapidocr) — TDD

**Files:**
- Create: `tests/test_introspection_extract_screen_text.py`
- Edit: `extensions/coding-harness/introspection/ocr.py`
- Edit: `extensions/coding-harness/introspection/tools.py`

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_introspection_extract_screen_text.py"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from extensions.coding_harness.introspection.tools import ExtractScreenTextTool
from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_joined_ocr_text():
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        return_value="Hello World\nLine 2",
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert not result.is_error
    assert "Hello World" in result.content
    assert "Line 2" in result.content


@pytest.mark.asyncio
async def test_handles_ocr_error():
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        side_effect=RuntimeError("rapidocr missing"),
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert result.is_error
    assert "rapidocr missing" in result.content


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ExtractScreenTextTool.capability_claims
    assert claims[0].capability_id == "introspection.extract_screen_text"
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_introspection_extract_screen_text.py -v` → FAIL (NotImplementedError from ocr.py).

- [ ] **Step 3: Implement ocr.py**

```python
"""OCR backend for ExtractScreenTextTool.

Default backend: rapidocr-onnxruntime (cross-platform pure-pip wheel).
"""

from __future__ import annotations

import io


def ocr_text_from_screen() -> str:
    """Capture the primary screen and OCR it; return joined text."""
    # Lazy imports — let users without OCR deps avoid startup cost.
    import mss
    import mss.tools
    from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        png = mss.tools.to_png(shot.rgb, shot.size)

    ocr = RapidOCR()
    result, _elapsed = ocr(io.BytesIO(png))
    if not result:
        return ""

    return "\n".join(line[1] for line in result if len(line) >= 2)
```

- [ ] **Step 4: Implement ExtractScreenTextTool.execute()**

Add at module top: `from extensions.coding_harness.introspection.ocr import ocr_text_from_screen`

```python
async def execute(self, call: ToolCall) -> ToolResult:
    try:
        text = ocr_text_from_screen()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
    return ToolResult(tool_call_id=call.id, content=text)
```

Schema: drop "Requires Tesseract on PATH" caveat — replace with "Cross-platform via rapidocr-onnxruntime (no system OCR install required)."

- [ ] **Step 5: Run, expect pass**

Run: `.venv/bin/pytest tests/test_introspection_extract_screen_text.py -v` → 3 PASSED.

- [ ] **Step 6: Commit**

```bash
git add tests/test_introspection_extract_screen_text.py extensions/coding-harness/introspection/tools.py extensions/coding-harness/introspection/ocr.py
git commit -m "feat(introspection): ExtractScreenTextTool via rapidocr (no system Tesseract)"
```

---

### Task 7: Add capability-claims namespace test, then swap plugin.py registration

**Files:**
- Create: `tests/test_introspection_capability_claims.py`
- Edit: `extensions/coding-harness/plugin.py`

- [ ] **Step 1: Write capability-claims contract test**

```python
"""tests/test_introspection_capability_claims.py"""
from __future__ import annotations

from extensions.coding_harness.introspection import ALL_TOOLS
from plugin_sdk.consent import ConsentTier


def test_all_tools_declare_introspection_namespace():
    for cls in ALL_TOOLS:
        claims = getattr(cls, "capability_claims", ())
        assert len(claims) == 1, f"{cls.__name__} must declare exactly one capability claim"
        assert claims[0].capability_id.startswith("introspection."), (
            f"{cls.__name__} claim {claims[0].capability_id!r} must use 'introspection.*' namespace"
        )
        assert claims[0].tier_required == ConsentTier.IMPLICIT, (
            f"{cls.__name__} should remain IMPLICIT (parity with prior oi_bridge)"
        )


def test_all_tools_have_unique_schema_names():
    names = set()
    for cls in ALL_TOOLS:
        # build with no args — registration uses no-arg constructor
        tool = cls()
        names.add(tool.schema.name)
    assert len(names) == 5, "Schema names must be unique"
```

- [ ] **Step 2: Run, expect pass**

Run: `.venv/bin/pytest tests/test_introspection_capability_claims.py -v` → 2 PASSED.

- [ ] **Step 3: Read current plugin.py**

Read `extensions/coding-harness/plugin.py`. Locate the OI registration block (lines ~163-212 from spec exploration).

- [ ] **Step 4: Replace OI registration with introspection registration**

Swap the entire `try/except`-wrapped OI block with:

```python
# Native introspection tools (replaces former oi_bridge subprocess wrapper)
try:
    from extensions.coding_harness.introspection import ALL_TOOLS as _INTROSPECTION_TOOLS
    for tool_cls in _INTROSPECTION_TOOLS:
        try:
            api.register_tool(tool_cls())
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to register introspection tool %s: %s", tool_cls.__name__, exc)
except ImportError as exc:
    _log.warning("Introspection module not loadable: %s", exc)
```

Remove any now-unused imports / module-alias synthesis lines that referenced `oi_bridge` or `extensions.coding_harness`.

- [ ] **Step 5: Verify plugin still loads**

Run: `.venv/bin/python -c "from extensions.coding_harness import plugin; print(plugin.__name__)"`
Expected: prints module name without exception.

- [ ] **Step 6: Run capability-claims test + tools tests together**

Run: `.venv/bin/pytest tests/test_introspection_*.py -v`
Expected: all introspection tests pass.

- [ ] **Step 7: Commit**

```bash
git add tests/test_introspection_capability_claims.py extensions/coding-harness/plugin.py
git commit -m "feat(introspection): swap plugin registration from oi_bridge → introspection"
```

---

### Task 8: Delete oi_bridge + oi-capability + 5 OI test files + conftest cleanup

**Files (deletes):**
- `extensions/coding-harness/oi_bridge/` (whole tree)
- `extensions/oi-capability/` (whole tree)
- `tests/test_coding_harness_oi_subprocess_wrapper.py`
- `tests/test_coding_harness_oi_venv_bootstrap.py`
- `tests/test_coding_harness_oi_protocol.py`
- `tests/test_coding_harness_oi_telemetry_disable.py`
- `tests/test_coding_harness_oi_agpl_boundary.py`
- `tests/test_coding_harness_oi_tools_tier_1_introspection.py`

**Files (edits):**
- `tests/conftest.py` — strip OI alias plumbing
- `tests/test_sub_f1_license_boundary.py` — drop OI-specific assertions

- [ ] **Step 1: Read tests/conftest.py to identify removable blocks**

Read `tests/conftest.py`. Identify:
- `_register_oi_capability_alias()` function and its imports (~30 LOC)
- `_register_oi_bridge_alias()` function (~40 LOC)
- The 2 module-level call sites
- Any imports / constants only used by those functions

- [ ] **Step 2: Strip OI plumbing from conftest.py**

Remove the two helper functions and their call sites. Re-read to confirm no unused imports remain.

- [ ] **Step 3: Read tests/test_sub_f1_license_boundary.py**

Identify any OI-specific grep / assertion lines.

- [ ] **Step 4: Drop OI-specific assertions; keep general F1 boundary checks**

Remove `import interpreter`-grep assertions specific to OI. The general F1 license-boundary check (no AGPL imports anywhere in plugin code) becomes a pure "check we don't import known AGPL deps" sweep — leave it but parameterize the deny-list to be empty if OI was the only one.

If no other AGPL deps are tracked, the test becomes vacuous — remove it cleanly. Or, keep it as a forward-looking guard with an empty denylist + a comment explaining the intent.

- [ ] **Step 5: Delete 6 OI test files + 2 directories**

```bash
rm -rf extensions/coding-harness/oi_bridge/
rm -rf extensions/oi-capability/
rm tests/test_coding_harness_oi_subprocess_wrapper.py
rm tests/test_coding_harness_oi_venv_bootstrap.py
rm tests/test_coding_harness_oi_protocol.py
rm tests/test_coding_harness_oi_telemetry_disable.py
rm tests/test_coding_harness_oi_agpl_boundary.py
rm tests/test_coding_harness_oi_tools_tier_1_introspection.py
```

- [ ] **Step 6: Run full pytest to ensure nothing else broke**

Run: `.venv/bin/pytest tests/ -x -q 2>&1 | tail -20`
Expected: all green (or only failures unrelated to OI removal).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore(oi): delete oi_bridge + oi-capability + 6 OI test files + conftest cleanup"
```

---

### Task 9: Update remaining docstring references

**Files:**
- Edit: `opencomputer/security/sanitize.py:50`
- Edit: `opencomputer/mcp/server.py:262`
- Edit: `extensions/coding-harness/tools/point_click.py`
- Edit: `tests/test_tool_descriptions_audit.py:144`

- [ ] **Step 1: sanitize.py**

Find the docstring at line ~50 referencing `"oi_bridge"`. Replace with `"introspection"`.

- [ ] **Step 2: mcp/server.py**

Find the docstring at line ~262 referencing `"oi_bridge.screenshot"`. Replace with `"introspection.screenshot"`.

- [ ] **Step 3: point_click.py**

Read the file. Find docstring referencing OI's `display.view` / `ScreenshotTool`. Update to point at the new `introspection` module.

- [ ] **Step 4: test_tool_descriptions_audit.py**

Find line ~144 with `"extensions.coding_harness.oi_bridge.tools.tier_1_introspection"`. Replace with `"extensions.coding_harness.introspection.tools"`.

- [ ] **Step 5: Run impacted tests**

Run: `.venv/bin/pytest tests/test_tool_descriptions_audit.py tests/test_instruction_detector.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/security/sanitize.py opencomputer/mcp/server.py extensions/coding-harness/tools/point_click.py tests/test_tool_descriptions_audit.py
git commit -m "chore(docs): update remaining oi_bridge → introspection references"
```

---

### Task 10: Doctor checks

**Files:**
- Edit: `opencomputer/doctor.py`
- Create: `tests/test_doctor_introspection_checks.py`

- [ ] **Step 1: Read current doctor.py**

Locate the existing check-registration pattern (likely a list of check functions or a registry).

- [ ] **Step 2: Write the failing test**

```python
"""tests/test_doctor_introspection_checks.py"""
from __future__ import annotations

import sys
from unittest.mock import patch

from opencomputer.doctor import _check_introspection_deps, _check_orphan_oi_venv


def test_orphan_oi_venv_detected(tmp_path):
    (tmp_path / "oi_capability").mkdir()
    result = _check_orphan_oi_venv(tmp_path)
    assert not result.ok
    assert "oi_capability" in result.message


def test_no_orphan_when_absent(tmp_path):
    result = _check_orphan_oi_venv(tmp_path)
    assert result.ok


def test_introspection_deps_check_succeeds_when_all_importable():
    results = _check_introspection_deps()
    # Each dep should produce one CheckResult; if any fail, message identifies which.
    failed = [r for r in results if not r.ok]
    if failed:
        pytest.fail("Missing deps: " + "; ".join(r.message for r in failed))
```

- [ ] **Step 3: Run, expect fail (functions don't exist yet)**

Run: `.venv/bin/pytest tests/test_doctor_introspection_checks.py -v` → FAIL.

- [ ] **Step 4: Implement the two helper functions**

Add to `opencomputer/doctor.py` (using existing `CheckResult` pattern from the file):

```python
import shutil

def _check_orphan_oi_venv(profile_home: Path) -> CheckResult:
    """Detect leftover OI venv directory from prior versions."""
    oi_venv = profile_home / "oi_capability"
    if oi_venv.exists():
        return CheckResult(
            ok=False,
            level="warning",
            message=f"Orphan OI venv at {oi_venv} (~150 MB). Safe to delete: rm -rf {oi_venv}",
        )
    return CheckResult(ok=True, level="info", message="No orphan OI venv")


def _check_introspection_deps() -> list[CheckResult]:
    results = []
    for mod_name in ("psutil", "mss", "pyperclip", "rapidocr_onnxruntime"):
        try:
            __import__(mod_name)
            results.append(CheckResult(ok=True, level="info", message=f"{mod_name} OK"))
        except ImportError:
            results.append(CheckResult(
                ok=False,
                level="error",
                message=f"{mod_name} missing — pip install -U {mod_name.replace('_', '-')}",
            ))
    if sys.platform.startswith("linux"):
        if shutil.which("xclip") or shutil.which("xsel"):
            results.append(CheckResult(ok=True, level="info", message="Linux clipboard helper present"))
        else:
            results.append(CheckResult(
                ok=False,
                level="warning",
                message="Linux clipboard requires xclip or xsel — apt install xclip",
            ))
    return results
```

Wire both into the main doctor entry point so they fire during `opencomputer doctor`.

- [ ] **Step 5: Run, expect pass**

Run: `.venv/bin/pytest tests/test_doctor_introspection_checks.py -v` → 3 PASSED.

- [ ] **Step 6: Smoke-test the CLI**

Run: `.venv/bin/opencomputer doctor 2>&1 | grep -iE "introspection|oi_capability"`
Expected: doctor emits the new checks without crash.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/doctor.py tests/test_doctor_introspection_checks.py
git commit -m "feat(doctor): orphan OI venv detection + introspection deps check"
```

---

### Task 11: Documentation cleanup (delete docs/f7/ + update CLAUDE.md)

**Files:**
- Delete: `OpenComputer/docs/f7/` (whole tree)
- Edit: `OpenComputer/CLAUDE.md` §4 phase table

- [ ] **Step 1: List docs/f7/ contents**

Run: `ls OpenComputer/docs/f7/ 2>/dev/null` to confirm files exist.

- [ ] **Step 2: Delete docs/f7/**

```bash
rm -rf OpenComputer/docs/f7/
```

- [ ] **Step 3: Read CLAUDE.md §4**

Identify any rows referencing oi-capability, oi_bridge, F7. Update to reflect the new introspection module.

- [ ] **Step 4: Update CLAUDE.md (minimal — only the lines that mention OI)**

Edit only the rows that mention F7 / oi-capability / oi_bridge. Don't rewrite the rest. Add a single new line under the v1.0-candidate phase notes mentioning the migration:

> **2026-04-27** — `oi_bridge` (Open Interpreter subprocess) replaced by native cross-platform `extensions/coding-harness/introspection/` module (psutil/mss/pyperclip/rapidocr-onnxruntime). 5 tool names preserved; F1 capability namespace migrated `oi_bridge.*` → `introspection.*`.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/docs/f7 OpenComputer/CLAUDE.md
git commit -m "chore(docs): remove docs/f7 + note OI removal in CLAUDE.md"
```

---

### Task 12: Final validation + CHANGELOG + push + PR

**Files:**
- Edit: `OpenComputer/CHANGELOG.md`

- [ ] **Step 1: Run full test suite**

Run: `cd /tmp/oc-oi-removal && .venv/bin/pytest tests/ -x -q 2>&1 | tail -20`
Expected: all green.

- [ ] **Step 2: Run ruff**

Run: `ruff check . 2>&1 | tail -5`
Expected: "All checks passed!"

- [ ] **Step 3: Add CHANGELOG entry**

Insert under `[Unreleased]`:

```markdown
### Removed (Open Interpreter subprocess bridge)

OpenComputer is MIT-licensed and must run on macOS, Linux, and Windows. The
prior `oi_bridge` design — subprocess RPC into AGPL Open Interpreter, behind a
2-test CI license boundary — paid a heavy structural cost (separate venv,
JSON-RPC, telemetry kill-switch) for what is, in the end, 5 thin wrappers
around `psutil`, `mss`, `pyperclip`, and an OCR engine. This change cuts the
bridge entirely.

- Deleted `extensions/coding-harness/oi_bridge/` (~1,308 LOC) — subprocess
  wrapper, venv bootstrap, JSON-RPC protocol, telemetry kill-switch, the 5
  Tier-1 OI-backed tools.
- Deleted `extensions/oi-capability/` (deprecated shim, no-op since
  2026-04-25).
- Deleted 6 OI test files (~1,220 LOC): subprocess_wrapper, venv_bootstrap,
  protocol, telemetry_disable, agpl_boundary, tools_tier_1_introspection.
- Deleted `tests/conftest.py` OI alias plumbing (~80 LOC).
- Deleted `OpenComputer/docs/f7/` design + interweaving plan documents
  (obsolete).

### Added (Native cross-platform introspection module)

New `extensions/coding-harness/introspection/` (3 files, ~300 LOC) provides
the SAME 5 tool names with native-Python implementations:

- `list_app_usage` → `psutil.process_iter` (cross-platform; replaces broken
  `ps aux` on Windows).
- `read_clipboard_once` → `pyperclip.paste()` (mac/linux/win out-of-the-box;
  Linux needs `xclip` or `xsel` — doctor warns).
- `screenshot` → `mss.mss().grab(...)` returning base64 PNG (faster than
  pyautogui, fewer system deps).
- `extract_screen_text` → `mss` capture + `rapidocr-onnxruntime` (no system
  Tesseract install).
- `list_recent_files` → `os.walk` + `pathlib` mtime filter + sort
  (cross-platform; replaces broken `find -mmin` on Windows).

Dependencies added to `pyproject.toml`: `psutil>=5.9`, `mss>=9.0`,
`pyperclip>=1.8`, `rapidocr-onnxruntime>=1.4` — all pure-pip wheels.

### Changed (F1 capability namespace migration)

Capability claims migrated from `oi_bridge.*` → `introspection.*`. The HMAC-
chained F1 audit log is unaffected (chain integrity is signature-based, not
namespace-based). Existing user grants under the old namespace become
orphans; users will be re-prompted at first use under the new namespace.
F1 only shipped 2 days prior — minimal blast radius.

### Added (Doctor checks)

- `_check_orphan_oi_venv` warns when a leftover
  `<profile_home>/oi_capability/` directory is detected (~150 MB), prompts
  the user to `rm -rf`.
- `_check_introspection_deps` verifies psutil / mss / pyperclip /
  rapidocr-onnxruntime are importable and (on Linux) that `xclip` or `xsel`
  is on PATH.

### Net diff: roughly −2,400 LOC

The OI bridge + 6 test files + conftest plumbing + docs/f7 totalled
~2,800 LOC. The native module + new tests come to ~600 LOC. Cross-platform
support went from "macOS, Linux only — Windows broken for 2 of 5 tools" to
"macOS, Linux, Windows out of the box".
```

- [ ] **Step 4: Final ruff + pytest pass**

Run: `ruff check . && .venv/bin/pytest tests/ -q 2>&1 | tail -5`
Expected: ruff clean + all tests pass.

- [ ] **Step 5: Push**

```bash
git push -u origin feat/native-cross-platform-introspection
```

- [ ] **Step 6: Open PR**

```bash
gh pr create --base main --title "feat: replace OI bridge with native cross-platform introspection (~−2,400 LOC)" --body "$(cat <<'EOF'
## Summary

Replaces the AGPL Open Interpreter subprocess bridge with a native pure-pip
cross-platform introspection module. Same 5 tool names, better Windows
support, no AGPL exposure, ~−2,400 LOC net.

- `extensions/coding-harness/oi_bridge/` (1,308 LOC) → `extensions/coding-harness/introspection/` (~300 LOC)
- 6 OI test files (~1,220 LOC) deleted; 6 new tests (~250 LOC)
- Deps: `psutil`, `mss`, `pyperclip`, `rapidocr-onnxruntime` (all pure-pip wheels)
- F1 capability namespace migrated `oi_bridge.*` → `introspection.*`
- Doctor: orphan-venv detection + dep verification (Linux xclip/xsel hint)

See `OpenComputer/docs/superpowers/specs/2026-04-27-oi-removal-native-introspection-design.md` for full design rationale.

## Test plan

- [x] `ruff check .` clean
- [x] `pytest tests/` full suite green
- [x] All 5 introspection tools have unit tests (mocked deps)
- [x] Capability-claims namespace test enforces `introspection.*` prefix
- [x] Doctor checks for orphan OI venv + introspection deps
- [x] CHANGELOG entry under `[Unreleased]`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: After CI green, squash-merge**

```bash
gh pr merge <PR#> --squash --delete-branch
```

---

## Self-Audit (writing-plans skill section)

### 1. Spec coverage

- §3.1 Module shape — covered by Task 1.
- §3.2 Tool implementations — covered by Tasks 2-6.
- §3.3 Dependencies — covered by Task 1.
- §3.4 Capability claims — covered by Tasks 2-6 (each declares new namespace) + Task 7 contract test.
- §4.1 Files deleted — covered by Task 8 + Task 11.
- §4.2 Files updated — covered by Task 7 (plugin.py), Task 8 (conftest.py), Task 9 (4 docstring sites), Task 11 (CLAUDE.md).
- §4.3 Doctor checks — covered by Task 10.
- §4.4 What survives — implicitly preserved by keeping tool names in Tasks 2-6.
- §5 Risks — each addressed: rapidocr lazy-import (T6), pyperclip Linux warn (T10), psutil field guards (T2), mss monitor[1] (T4), F1 namespace re-prompt (acknowledged in CHANGELOG entry T12), orphan venv (T10), Windows path separators (`pathlib` is OS-correct in T5).
- §6 Out of scope — explicitly defers Apple Vision OCR, multi-monitor, knowledgeC.db focus tracking, Tier 2-5 reintroduction. None of these are in any task.

**Gap check:** §4.2 lists `extensions/coding-harness/tools/point_click.py` as needing a docstring update — covered by Task 9 Step 3.

### 2. Placeholder scan

- No "TBD" / "TODO" / "fill in details" anywhere in the plan.
- Every task has concrete file paths and runnable commands.
- Test code is complete (no "// add tests" placeholders).
- Implementation snippets are complete enough to compile (imports + signature + body).

### 3. Type / signature consistency

- `ToolCall(id=..., name=..., arguments=...)` used consistently across all tests (T2-T7).
- `ToolResult(tool_call_id=call.id, content=..., is_error=...)` used consistently across all `execute()` implementations.
- `CapabilityClaim` signature `(capability_id=..., tier_required=..., human_description=...)` consistent (verified against existing oi_bridge code).
- `ConsentTier.IMPLICIT` consistent (matches existing oi_bridge claims).
- Schema parameter dicts use the same JSON-schema shape as existing tools (`{"type": "object", "properties": {...}, "required": []}`).
- Function signatures match between spec and plan: `_quadrant_bounds(monitor, quadrant)`, `_walk_recent_files(base, cutoff, limit)`, `ocr_text_from_screen()`.

### 4. Adversarial / red-team self-critique

**(Per the user's directive: "rigorously audit your own output as an expert critic; identify flawed assumptions, edge cases, missing considerations, alternative approaches; stress-test the plan against real-world constraints.")**

#### 4.1 Flawed assumptions

| # | Assumption | Reality / Mitigation |
|---|---|---|
| FA1 | "rapidocr-onnxruntime is pure-pip and works the same on all 3 OSes." | True for Linux + macOS. On Windows, ONNX Runtime needs the right CPU instruction set (AVX2 typical). Should hold for 99% of modern Windows machines. **Mitigation**: doctor explicitly imports rapidocr to surface ONNX runtime failures. |
| FA2 | "psutil's `cpu_percent` field is meaningful from `process_iter`." | psutil's `cpu_percent()` returns 0.0 on the FIRST call — it's a sampling delta. To get real values, you call once to prime, sleep, then call again. The `process_iter(['cpu_percent'])` approach gives a snapshot that may be 0.0 for many processes. **Mitigation**: in the implementation, we'd need either a 100ms warmup interval (`psutil.cpu_percent(interval=0.1)` at module level) or accept that values are noisy. **Action**: update T2 to add a `psutil.cpu_percent(interval=None)` priming call before iteration; document in tool description. |
| FA3 | "list_recent_files with pathlib will be fast on user homes." | Walking ~/ with millions of files (Mail caches, Library, node_modules) is SLOW even with skip-dirs. **Mitigation**: T5 has `_SKIP_DIR_NAMES` covering common bloat dirs. Add to skip-list: `Library/Mail`, `Library/Caches` (macOS), `AppData/Local` (Windows). Also: hard-cap walked file count (e.g., 50,000) before bailing — return whatever was found. |
| FA4 | "mss works headless on Linux without X server." | mss needs X (or Wayland-compatible Xwayland). On a Linux server with no display, screenshot fails. **Mitigation**: doctor adds a Linux check for `DISPLAY` / `WAYLAND_DISPLAY` env var; tool returns clear error if unavailable. Acceptable: introspection tools are meaningless without a graphical session. |
| FA5 | "F1 capability migration is risk-free because F1 is 2 days old." | Memory check: F1 audit log has HMAC chain. Old entries reference `oi_bridge.X` capabilities. The chain VERIFIES — it just describes a now-unknown namespace. Tooling that reads the audit log might be confused by ghost names. **Mitigation**: add a one-time CLI subcommand `opencomputer consent migrate-oi-namespace` that walks the audit log and emits a "the following capability_ids are obsolete" report. Optional. **Defer**: out of scope for v1; document in CHANGELOG. |
| FA6 | "Apple Vision framework would always beat rapidocr accuracy." | Vision is solid for clean text; rapidocr is comparable for screen UI text and outperforms on Asian scripts (CJK). Defer-to-rapidocr is fine. |
| FA7 | "Schema descriptions don't matter much for cross-platform." | The CURRENT schemas claim "Windows not supported" — if the LLM has been told that, it may refuse to call the tool on Windows. **Mitigation**: T2 + T5 explicitly update the schema description (already in the plan). Verify: search the repo for the literal string "Windows not supported" before T12 to ensure no stale copies remain elsewhere. |

#### 4.2 Edge cases

| # | Edge case | Mitigation |
|---|---|---|
| EC1 | User with `/.opencomputer/<profile>/oi_capability/venv/` running an in-flight session — they upgrade and the venv is suddenly orphaned mid-session. | Doctor flags it; venv stays on disk until explicit `rm -rf`. No mid-session crash because the new tools don't depend on the old venv. |
| EC2 | User is on Windows and pyperclip works *partially* — strings yes, files/images no. | Tool docstring + return value handle string-only case. Failure mode is clean error. |
| EC3 | Screenshot >10 MB base64 chokes the LLM context window. | Existing tool result spillover (TS-T2 from prior session) handles this — large results spill to `<profile_home>/tool_result_storage/` and the model sees a placeholder + path. Inherited behavior; nothing new to do. |
| EC4 | OCR returns CJK characters; LLM is fine, but old tests asserting English-only break. | New tests don't assert specific language. |
| EC5 | rapidocr first-time-import downloads ONNX model from PyPI ONLY (not separate). | rapidocr-onnxruntime ships the model in the wheel — no runtime download. Wheel size ~70-100 MB. Acceptable. |
| EC6 | A symlink loop in user's home directory hangs `os.walk`. | T5 implementation uses `os.walk`'s default `followlinks=False`. Safe. |
| EC7 | Permissions error reading a file's `stat()` (macOS Recently-Used SQLite locked, etc.). | T5 implementation wraps `p.stat()` in try/except OSError — skips the file, continues. |
| EC8 | mss.mss() inside `with` block fails to close on exception → Linux X resource leak. | mss's context manager cleans up; the `with` block in T4 implementation handles it. |
| EC9 | Two tools called in parallel that both spawn rapidocr → memory pressure. | rapidocr instance is created per call, not shared. ~200 MB per instance. Two parallel calls = 400 MB transient. Acceptable; declare `parallel_safe=False` on `ExtractScreenTextTool` to serialize via the registry. **Action**: update T6 implementation to set `parallel_safe = False`. |
| EC10 | `psutil.process_iter()` raises `psutil.AccessDenied` on Windows for some processes. | psutil's `info` dict access already guards via `info.get(...)` in T2 — the iteration itself can also raise; T2 wraps the whole loop in try/except. |

#### 4.3 Missing considerations

| # | Missing consideration | Action |
|---|---|---|
| MC1 | **No CI matrix entry for Windows or macOS.** Without that, "cross-platform" is asserted but not verified in CI. | Out of scope this PR — but propose a follow-up: add `runs-on: [ubuntu-latest, macos-latest, windows-latest]` to the test workflow. Document as a follow-up in CHANGELOG. |
| MC2 | **Plugin SDK boundary test still scans `extensions/coding-harness/oi_bridge/`** if it walked filesystem at test time. | The SDK boundary test (`tests/test_phase6a.py`) reads `plugin_sdk/`, not extensions. Verified — not affected. |
| MC3 | **`parallel_safe` for `ExtractScreenTextTool`.** Current oi_bridge ScreenTextTool sets `parallel_safe = True`. With rapidocr instantiation per-call (200 MB), parallel is risky. | Update T6 implementation: `parallel_safe = False`. Add a comment explaining why. |
| MC4 | **Lazy-import in `tools.py`** — top-level `import psutil, mss, pyperclip` means importing the introspection module forces these deps. If a user installs OC without coding-harness extras, the module load might fail. | The whole point is that they're now in `pyproject.toml [project.dependencies]` — not optional. Plus, the `try/except ImportError` wrapper in plugin.py handles registration failure gracefully. Acceptable. |
| MC5 | **Order of tasks: T8 deletes oi_bridge BEFORE T7 finishes the registration swap?** Re-reading: T7 does the swap, then T8 deletes. Correct order. ✓ |
| MC6 | **`tests/conftest.py` strip in T8** — must NOT break unrelated tests that share the conftest. | Conftest is read at test-collection time. Other fixtures should be untouched. Verified by full pytest run in T8 Step 6. |
| MC7 | **`pyproject.toml` deps order** — keeping alphabetical / grouped consistency matters for diffs. | T1 Step 2 — note in implementation: respect existing deps section structure. |
| MC8 | **The dogfood gate (CLAUDE.md §5)** — does this PR violate it? | Saksham hasn't done the 2-week dogfood. But this PR is technical debt cleanup, not feature expansion. It removes a license risk + bug (Windows broken). Justified. Document in PR body. |
| MC9 | **AGPL boundary tests deletion**. Removing them is fine for OI but a future AGPL dep would have no guard. | Replace `test_coding_harness_oi_agpl_boundary.py` with a generic `tests/test_no_agpl_imports.py` that scans for known AGPL packages (`interpreter`, `agpl-locked-package-X`, etc.) by importable name. Default deny-list: `interpreter` (just in case it returns). **Action**: split off as T11.5 (small extra task). |
| MC10 | **rapidocr GPU path**. rapidocr-onnxruntime CPU is fine for desktop; rapidocr-paddle / rapidocr-openvino exist. Not in scope. | Defer. CPU is the right default for one-off OCR calls. |
| MC11 | **Handling deleted `extensions.oi_capability` import in user-installed plugins**. Anyone who shipped a plugin importing `extensions.oi_capability.*` breaks. | Search for that import outside our repo: not a concern for now (no third-party plugins). For local plugins, the deprecation shim was a no-op since 2026-04-25 — anything that survived must have stopped using it. Acceptable. |
| MC12 | **`opencomputer doctor` exit code semantics**. Adding new failing checks (Linux xclip missing) might make doctor return non-zero where it didn't before. | Doctor already handles warnings vs errors via `level` field. Add the new checks at `level="warning"` (not "error") so they don't break automation depending on doctor exit code. ✓ already specified in T10. |

#### 4.4 Alternatives considered + rejected

| # | Alternative | Why not |
|---|---|---|
| A1 | Keep OI for OCR + app-usage only (Option B from prior analysis). | Higher complexity for no meaningful benefit. AGPL boundary remains. |
| A2 | Use `pyautogui` + `pillow` instead of `mss`. | Slower; bigger transitive deps; pyautogui is GUI-automation focused (mouse/keyboard) which we don't want exposed at all. |
| A3 | Use Tesseract via `pytesseract` instead of rapidocr. | Requires system Tesseract install — exactly what we're trying to escape. |
| A4 | Don't replace OI — just stop using it (keep stubs). | Dead-code accumulation. Worse than full removal. |
| A5 | Move tools out of coding-harness into core `opencomputer/tools/`. | Violates the "chat agent vs coding agent" separation. Coding-harness toggle is intentional. |
| A6 | Write platform-specific implementations per OS (separate macos.py / linux.py / windows.py). | Over-engineered for 5 simple tools. The chosen libraries already abstract this. |
| A7 | Ship an `--agpl-allowed` flag and keep OI as opt-in. | User's MIT posture is the constraint; AGPL opt-in is a foot-gun for downstream. |

#### 4.5 Refinements applied to plan from self-audit

1. **T2 — add `psutil.cpu_percent(interval=None)` priming call** before iteration so values aren't all 0.0. (FA2)
2. **T5 — extend `_SKIP_DIR_NAMES`** with Mac/Win-specific bloat dirs (`Library/Mail`, `Library/Caches`, `AppData/Local`). (FA3)
3. **T5 — add hard-cap on walked file count** (e.g., 50,000) before bailing. (FA3)
4. **T6 — set `parallel_safe = False`** on `ExtractScreenTextTool`. (MC3, EC9)
5. **Add T11.5 — generic `tests/test_no_agpl_imports.py`** sweep, replacing the OI-specific boundary test. (MC9)
6. **T9 — grep repo** for any remaining "Windows not supported" string before commit. (FA7)
7. **Doctor: Linux check is `level="warning"` not "error"** so doctor exit code stays clean. (MC12)
8. **T1 Step 2 note**: respect existing deps order in pyproject.toml. (MC7)

### 5. Effort estimate (post-audit)

| Task | Implementation | Tests | Total |
|---|---|---|---|
| T1 module skeleton | 20m | — | 20m |
| T2 list_app_usage | 30m | 20m | 50m |
| T3 read_clipboard | 15m | 15m | 30m |
| T4 screenshot | 30m | 25m | 55m |
| T5 list_recent_files | 30m | 25m | 55m |
| T6 extract_screen_text | 40m | 25m | 65m |
| T7 plugin.py swap | 25m | 15m | 40m |
| T8 deletes + conftest | 25m | (verify only) | 25m |
| T9 docstring updates | 15m | (verify only) | 15m |
| T10 doctor checks | 30m | 20m | 50m |
| T11 docs cleanup | 15m | — | 15m |
| T11.5 generic AGPL guard | 20m | 10m | 30m |
| T12 CHANGELOG + push + PR | 30m | (CI runs) | 30m |
| **Total** | | | **~7 hours** |

With subagent-driven dev (parallel implementer + reviewer cycles, fresh subagent per task), realistic ship: **~6-7 hours of wall-clock**.

### 6. Acceptance criteria (the merge bar)

- [ ] `ruff check .` clean.
- [ ] Full pytest green on Python 3.12 + 3.13.
- [ ] All 5 introspection tools have unit tests (mocked deps).
- [ ] Capability-claims namespace test enforces `introspection.*` prefix on all 5.
- [ ] No `oi_bridge`, `oi_capability`, `interpreter` imports in non-deleted code (grep verified).
- [ ] No "Windows not supported" string in any tool schema description.
- [ ] CHANGELOG entry under `[Unreleased]` with the 4 sections (Removed, Added native module, Changed F1 namespace, Added doctor checks).
- [ ] PR body includes link to spec + summary of net diff.
- [ ] CLAUDE.md §4 has a one-line entry for the migration.

---

*Plan complete. Ready for subagent-driven execution per the user's directive ("then after that you can move on to plan execution using /executing-plans").*
