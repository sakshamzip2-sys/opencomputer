# Native Cross-Platform Introspection — Replace Open Interpreter Bridge

**Date:** 2026-04-27
**Status:** Design (pre-implementation)
**Branch:** `feat/native-cross-platform-introspection`
**Worktree:** `/tmp/oc-oi-removal/`

---

## 1. Goal (one sentence)

Replace OpenComputer's Open Interpreter subprocess bridge with native pure-pip cross-platform Python (psutil + mss + pyperclip + rapidocr-onnxruntime) that ships the same 5 introspection tools (`list_app_usage`, `read_clipboard_once`, `screenshot`, `extract_screen_text`, `list_recent_files`) on macOS, Linux, AND Windows — with no AGPL exposure.

## 2. Why this exists

OpenComputer is an MIT-licensed open-source project that must run on **macOS, Linux, and Windows**. Today's OI bridge:

- **Cross-platform claim is broken** — the schemas for `list_app_usage` and `list_recent_files` already document "Platform: macOS, Linux. Windows not supported." That's because OI calls `ps aux` and `find -mmin -N` via shell, neither of which exist on Windows.
- **Carries AGPL contagion** — `interpreter` is AGPL-3.0. The current architecture quarantines it via subprocess + 2 CI grep tests + a separate venv. That defensive shape is itself a tell that this dependency doesn't fit the project's license posture.
- **Subprocess overhead is paid every call** — JSON-RPC roundtrip + venv Python + telemetry kill-switch + log file management per tool invocation, just to call `pyperclip.paste()` (one line of Python).
- **What OI uniquely contributed has shrunk** — Tiers 2-5 (browser/email/code-exec/scheduling) were trimmed in commit `27a9275` because OC's own primitives covered them. Tier 1 was trimmed 8→5 in `f635cc8`. The trim trajectory is clear: OI's value-add is dwindling.

After re-checking the rest of the codebase, the 5 Tier-1 tools are all **thin wrappers** over OI's `computer.X.Y(...)` calls, which are themselves thin wrappers over pyperclip/pyautogui/Tesseract. The native libraries are right there — we're paying a subprocess + AGPL premium for an indirection.

## 3. Architecture

### 3.1 Module shape

```
extensions/coding-harness/introspection/
├── __init__.py             # ALL_TOOLS export — same shape as oi_bridge/tools/__init__.py
├── tools.py                # 5 BaseTool subclasses (~250 LOC)
└── ocr.py                  # OCR helper — rapidocr default, optional pyobjc Vision on macOS
```

That's it — no `subprocess/`, no `venv_bootstrap`, no `telemetry_disable`, no `protocol`, no `wrapper`. The whole bridge subdirectory goes away.

### 3.2 Tool implementations (one paragraph each)

**`ListAppUsageTool`**
```python
import psutil
procs = [
    {"name": p.info["name"], "cpu_percent": p.info["cpu_percent"], "started": p.info["create_time"]}
    for p in psutil.process_iter(["name", "cpu_percent", "create_time"])
]
procs.sort(key=lambda p: p["cpu_percent"], reverse=True)
return procs[:30]
```
Cross-platform via psutil (mac/linux/win). Same semantics as today's `ps aux | sort -k10 -rn | head -30` but with a structured return value the model can reason about. Honors the existing `hours` parameter as a cutoff on `started` (filter to processes with `create_time > now - hours*3600`).

**`ReadClipboardOnceTool`**
```python
import pyperclip
return pyperclip.paste()
```
That's literally it. Linux: doctor warns if `xclip`/`xsel` missing.

**`ScreenshotTool`**
```python
import mss, base64
with mss.mss() as sct:
    monitor = sct.monitors[1]  # full primary monitor
    if quadrant:
        monitor = _quadrant_bounds(monitor, quadrant)
    img = sct.grab(monitor)
    png_bytes = mss.tools.to_png(img.rgb, img.size)
return base64.b64encode(png_bytes).decode("ascii")
```
Quadrant logic: bisect monitor width/height, return the appropriate quarter rect.

**`ExtractScreenTextTool`**
```python
from .ocr import ocr_text_from_screen
return ocr_text_from_screen()
```
The `ocr.py` module:
1. mss grab of full screen → in-memory PNG
2. On macOS with `pyobjc-framework-Vision` available: use Vision framework (highest accuracy, no model download).
3. Otherwise: rapidocr-onnxruntime (cross-platform, ships its own ONNX model — ~70 MB on first import).
4. Return joined text.

**`ListRecentFilesTool`**
```python
import os, time
from pathlib import Path

base = Path(os.path.expanduser(directory))
cutoff = time.time() - hours * 3600
results: list[tuple[float, Path]] = []
for p in base.rglob("*"):
    if not p.is_file() or _is_skipped_dir_in(p):  # skip .git, node_modules, .venv, __pycache__
        continue
    try:
        mtime = p.stat().st_mtime
    except OSError:
        continue
    if mtime > cutoff:
        results.append((mtime, p))
        if len(results) >= limit * 2:  # collect 2x then sort+trim
            break
results.sort(reverse=True)
return [{"path": str(p), "mtime": mtime} for mtime, p in results[:limit]]
```
Cross-platform via pathlib + os.stat. Skip-dirs avoids the slow-on-large-homes problem. The `limit*2` heuristic gives the sort enough headroom while still bounding work.

### 3.3 Dependencies

Add to `OpenComputer/pyproject.toml` `[project.dependencies]`:

```toml
psutil>=5.9
mss>=9.0
pyperclip>=1.8
rapidocr-onnxruntime>=1.4
```

These are all pure-pip wheels with broad platform support. No system Tesseract, no system shell utilities required (with the noted exception of `xclip`/`xsel` for Linux clipboard — already a system requirement for any clipboard solution on Linux).

**No optional macOS extras in this iteration.** `pyobjc-framework-Vision` would give higher OCR quality but adds 50+ MB of pyobjc dependencies; defer to a follow-up if rapidocr accuracy proves insufficient in dogfood.

### 3.4 Capability claims (F1)

Rename namespace from `oi_bridge.*` → `introspection.*`:

| Old | New |
|---|---|
| `oi_bridge.list_app_usage` | `introspection.list_app_usage` |
| `oi_bridge.read_clipboard_once` | `introspection.read_clipboard_once` |
| `oi_bridge.screenshot` | `introspection.screenshot` |
| `oi_bridge.extract_screen_text` | `introspection.extract_screen_text` |
| `oi_bridge.list_recent_files` | `introspection.list_recent_files` |

All remain `ConsentTier.IMPLICIT` (same risk profile). The F1 audit-log chain integrity (HMAC-based) is unaffected — old audit entries that reference `oi_bridge.X` remain valid signatures over their original payloads; the namespace string is just metadata. New tools register the new namespace at startup; users who previously granted `oi_bridge.*` will be re-prompted at first use under the new namespace. (Acceptable: F1 shipped 2 days ago, no production grant data.)

## 4. Migration & cleanup

### 4.1 Files / directories deleted

| Path | What it is | Action |
|---|---|---|
| `extensions/coding-harness/oi_bridge/` | Subprocess bridge (~1,308 LOC) | DELETE |
| `extensions/oi-capability/` | Deprecated shim (already no-op) | DELETE |
| `tests/test_coding_harness_oi_subprocess_wrapper.py` | (461 LOC) | DELETE |
| `tests/test_coding_harness_oi_venv_bootstrap.py` | (139 LOC) | DELETE |
| `tests/test_coding_harness_oi_protocol.py` | (164 LOC) | DELETE |
| `tests/test_coding_harness_oi_telemetry_disable.py` | (114 LOC) | DELETE |
| `tests/test_coding_harness_oi_agpl_boundary.py` | (121 LOC, AGPL grep CI guard) | DELETE |
| `tests/test_coding_harness_oi_tools_tier_1_introspection.py` | (185 LOC) | DELETE |
| `docs/f7/interweaving-plan.md` | OI integration plan (obsolete) | DELETE |
| `docs/f7/oi-source-map.md` | OI source map (obsolete) | DELETE |
| `docs/f7/design.md` | F7 design doc (obsolete) | DELETE |

### 4.2 Files updated (small edits)

| Path | Edit |
|---|---|
| `extensions/coding-harness/plugin.py` | Replace `oi_bridge` registration block with `introspection` registration |
| `tests/conftest.py` | Remove `_register_oi_capability_alias()` + `_register_oi_bridge_alias()` (~80 LOC) |
| `tests/test_sub_f1_license_boundary.py` | Remove OI-specific assertions; keep general F1 boundary checks |
| `tests/test_tool_descriptions_audit.py:144` | Update path `oi_bridge.tools.tier_1_introspection` → `introspection.tools` |
| `opencomputer/security/sanitize.py:50` | Update docstring example `"oi_bridge"` → `"introspection"` |
| `opencomputer/mcp/server.py:262` | Update docstring example `"oi_bridge.screenshot"` → `"introspection.screenshot"` |
| `extensions/coding-harness/tools/point_click.py` | Update docstring referencing OI's screenshot — point at new module |
| `OpenComputer/CLAUDE.md` §4 | Update phase table to reflect OI removal |
| `OpenComputer/CHANGELOG.md` | New entry under `[Unreleased]` |

### 4.3 Doctor checks added

```python
# opencomputer/doctor.py — new checks

def _check_orphan_oi_venv(profile_home: Path) -> CheckResult:
    """Detect leftover OI venv from prior versions; suggest cleanup."""
    oi_venv = profile_home / "oi_capability"
    if oi_venv.exists():
        return CheckResult(
            ok=False,
            level="warning",
            message=f"Orphan OI venv at {oi_venv} (~150MB). Safe to delete: rm -rf {oi_venv}",
        )
    return CheckResult(ok=True, level="info", message="No orphan OI venv")


def _check_introspection_deps() -> list[CheckResult]:
    """Verify introspection module's runtime deps."""
    results = []
    for mod in ("psutil", "mss", "pyperclip", "rapidocr_onnxruntime"):
        try:
            __import__(mod)
            results.append(CheckResult(ok=True, level="info", message=f"{mod} OK"))
        except ImportError as e:
            results.append(CheckResult(ok=False, level="error", message=f"{mod} missing: pip install {mod}"))

    # Linux clipboard: pyperclip needs xclip/xsel
    if sys.platform.startswith("linux"):
        if shutil.which("xclip") or shutil.which("xsel"):
            results.append(CheckResult(ok=True, level="info", message="Linux clipboard helper present"))
        else:
            results.append(CheckResult(
                ok=False, level="warning",
                message="Install xclip or xsel for clipboard support: apt install xclip",
            ))
    return results
```

### 4.4 What survives

- **Tool name contract** — all 5 LLM-facing tool names unchanged. Schemas updated to drop "Windows not supported" caveats and to remove "Requires Tesseract on PATH" note.
- **`extensions/coding-harness/` plugin boundary** — same activation toggle (install coding-harness → tools available; don't install → not available).
- **F1 ConsentGate** — still enforced, just at the new namespace.

## 5. Risks & edge cases

| Risk | Mitigation |
|---|---|
| rapidocr-onnxruntime ONNX model bundles ~70MB | Document in install instructions; deps install adds ~100MB total — still smaller than the OI venv (150MB+). Lazy-import in `ocr.py` so non-OCR users don't pay startup cost. |
| pyperclip on Linux without xclip/xsel | Doctor warns; tool returns clear error message routing user to install instructions; rest of toolkit unaffected. |
| psutil `process_iter` differs subtly per OS in field availability | Use only the universal fields (`name`, `cpu_percent`, `create_time`); guard each access. |
| mss multi-monitor on Linux/Windows | Default to monitor[1] (primary); document `monitor` parameter as a future expansion (out of scope for this PR). |
| Existing user has F1 grants under `oi_bridge.*` | Re-prompted at first use under new namespace. Acceptable churn — F1 only 2 days old. |
| Existing user has `~/.opencomputer/<profile>/oi_capability/venv/` (~150MB) | Doctor flags it; user runs `rm -rf` once; no auto-deletion (avoid surprises). |
| First-time rapidocr download blocks tool execution | Pre-warm via doctor: doctor importing rapidocr triggers model download. Document. |
| Windows path separators in `list_recent_files` | `pathlib.Path` handles this; just need to test with `\\` paths. |
| Schema breaking change for the LLM | None: tool names + parameter shapes preserved. Description text updated to drop Windows-not-supported caveats. |

## 6. Out of scope (defer)

- **Apple Vision OCR** — higher quality on macOS but adds pyobjc bulk. Defer to follow-up if rapidocr proves insufficient.
- **Multi-monitor screenshot parameter** — current OI implementation captures primary only; preserving that scope.
- **Native focus-time tracking via macOS `knowledgeC.db`** — would require platform-specific code + AppleScript permissions. The `psutil`-based "top by CPU" matches current OI behavior; promote to focus-time later if dogfood reveals demand.
- **Tier 2-5 reintroduction** — explicitly trimmed in 2026-04-25 (`27a9275`). If revived, do via dedicated tools (BashTool, WebFetch, cron) per the existing committed rationale, not via OI.

## 7. Self-review notes

**Placeholder scan:** none — all schemas, imports, classes named concretely.

**Internal consistency:** module structure (3 files), tool names (5), capability namespace migration (1:1), doctor checks (2) — counts match across §3 and §4.

**Scope check:** focused on the OI removal + native replacement. Doctor checks are a tightly-scoped addition to support the migration. CHANGELOG / CLAUDE.md updates are mandatory housekeeping. No feature creep.

**Ambiguity check:** OCR backend selection — spec defaults to rapidocr-onnxruntime cross-platform, with the optional macOS pyobjc Vision path **deferred**. Explicit so the implementer doesn't hedge.

**One-line summary for the plan doc:** "Replace OI subprocess bridge with native introspection module (psutil/mss/pyperclip/rapidocr-onnxruntime). Same 5 tools, better cross-platform, no AGPL, ~−2,000 LOC net."

---

*Spec ready for the writing-plans skill to expand into bite-sized tasks.*
