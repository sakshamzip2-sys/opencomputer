# Worked example — resolving a real ModuleNotFoundError

The following is a canonical trace and the diagnosis path this skill
produces. Use this to calibrate the level of detail to expect in an
actual debugging session.

## Input — the user pastes this

```
Traceback (most recent call last):
  File "/Users/alice/work/myproj/run.py", line 3, in <module>
    from myproj.core import pipeline
  File "/Users/alice/work/myproj/myproj/__init__.py", line 1, in <module>
    import httpx
ModuleNotFoundError: No module named 'httpx'
```

## Skill walkthrough

1. **Identify the error (SKILL.md §1).**
   - Missing module: `httpx`
   - Triggered by: `myproj/__init__.py` line 1
   - Not a partial match — it's a top-level third-party package.

2. **Run checklist A — missing dependency (SKILL.md §2.A).**

   ```bash
   pip list | grep -i httpx
   # (no output)
   ```

   Package not installed in the active interpreter.

3. **Check which interpreter is active.**

   ```bash
   which python     # /Users/alice/work/myproj/.venv/bin/python
   echo $VIRTUAL_ENV  # /Users/alice/work/myproj/.venv
   ```

   Venv is active. So the dependency is simply missing.

4. **Check if it should be declared.**

   ```bash
   grep -n httpx pyproject.toml
   # (no output)
   ```

   Not in pyproject. Two options:
   - If `httpx` is a real runtime dependency: add it to
     `pyproject.toml` under `dependencies` and reinstall the project.
   - If it's a transitive dep that should come from another package:
     find the declared dep that *should* pull it in (`pip show <that>`
     shows requirements), and fix that package's pinning.

5. **Apply the fix.**

   ```bash
   # Option A — add to declared deps
   echo 'Add "httpx>=0.27" to pyproject.toml dependencies, then:'
   pip install -e .

   # Option B — quick install for exploration (does NOT update deps)
   pip install httpx
   ```

6. **Verify (SKILL.md §4).**

   ```bash
   python -c "import httpx; print('OK', httpx.__version__)"
   python run.py  # the original failing command
   ```

7. **Save the fix if non-obvious (SKILL.md §5).**

   In this canonical case the fix is mundane — no new skill needed.
   If the root cause had been a circular import or a sys.path quirk,
   this is where you'd call `skill_manage` with `action="create"` to
   capture the recipe for next time.

## Decision tree in one sentence

"If `pip list` doesn't see it and the venv is active, the package is
simply missing — install it (or add it to your declared deps). If
`pip list` *does* see it but the import still fails, you've got the
wrong interpreter or a sys.path problem."
