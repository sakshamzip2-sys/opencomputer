---
name: debug-python-import-error
description: Use when the user hits a ModuleNotFoundError, ImportError, ImportError when running a Python script, circular import, "no module named X", or asks about fixing a broken Python import.
version: 0.1.0
---

# Debugging Python Import Errors

When the user hits an import error, follow this systematic checklist.

## 1. Identify the exact error

Look at the traceback — the key things to extract:
- Exact module name that failed (`ModuleNotFoundError: No module named 'foo'`)
- Which file triggered the import
- Any partial match (sometimes `X.Y` fails where `X` works — points to a missing submodule)

## 2. The 4 most common causes (check in order)

### A. Missing dependency
Most common. Package simply isn't installed.
- Check: `pip list | grep <module>` or `python -c "import <module>"`
- Fix: `pip install <module>` (or `pip install -e .` if editable in-project)

### B. Virtual environment not activated
The script is running against system Python, not the venv.
- Check: `which python` and `echo $VIRTUAL_ENV`
- Fix: `source .venv/bin/activate` (or equivalent)

### C. Wrong working directory / sys.path
Package lives somewhere not on `sys.path`.
- Check: `python -c "import sys; print(sys.path)"`
- Check: is the project installed with `pip install -e .`? That adds src to path.
- Fix: install in editable mode or use `PYTHONPATH=...`.

### D. Circular import
`A` imports `B` which imports `A` back.
- Signal: error says "partially initialized module" or "cannot import name X from Y"
- Fix: restructure — extract the shared piece into a third module that both import.

## 3. Plugin/extension specific (OpenComputer)

If the error is in an OpenComputer plugin:
- Plugin entry module names collide — check `opencomputer/plugins/loader.py` — we clear common names between plugin loads.
- `from X import Y` at top of plugin.py — use the try/except ImportError dual pattern or `importlib.util.spec_from_file_location`.

## 4. Verify the fix

After making a change:
```bash
python -c "import <module>; print('OK')"
# or run the failing script again
```

## 5. Save the root cause

If this was a non-obvious fix (circular import fix, sys.path trick), save it
to a skill so you don't debug it again next time.
