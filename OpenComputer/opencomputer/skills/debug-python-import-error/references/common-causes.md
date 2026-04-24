# Common causes of Python import errors (deep reference)

This is the long-form reference backing the short "4 most common causes"
checklist in SKILL.md. Load on-demand when the quick checklist in the
main skill isn't enough to resolve the issue.

## A. Missing dependency

**Symptom.** `ModuleNotFoundError: No module named 'foo'` where `foo` is a
third-party package you expected to be installed.

**Deeper diagnostics.**

1. `pip list | grep -i foo` — shows whether it's installed in the active
   interpreter's site-packages.
2. `python -c "import foo; print(foo.__file__)"` — when this succeeds,
   the path tells you which copy Python resolved.
3. `pip show foo` — gives you version + install location + dependents.
4. Check `pyproject.toml` / `requirements.txt` — is `foo` even declared?
   If not, adding it is the fix; if yes, the environment is out of sync
   with the declaration (`pip install -e .` or `pip install -r
   requirements.txt`).

**Less-obvious variant — wrong extra.** `pip install foo[extras]`
semantics: the base `foo` import works, but `foo.extra_subpackage`
fails because the extras group wasn't installed. Read the package's
`extras_require` in its setup.cfg / pyproject.toml to see which extras
gate which submodules.

## B. Virtual environment not activated

**Symptom.** `which python` shows `/usr/bin/python3` (or similar system
path) instead of the venv's `bin/python`. Packages you installed via
`.venv/bin/pip install foo` aren't visible.

**Deeper diagnostics.**

- `echo $VIRTUAL_ENV` — empty means no venv is active in this shell.
- `python -c "import sys; print(sys.prefix, sys.base_prefix)"` — if
  these are equal, you're on system Python, not a venv.
- When running scripts non-interactively (cron, systemd), activation
  scripts often aren't sourced. Invoke the venv's python directly by
  absolute path: `/path/to/.venv/bin/python script.py`.

## C. Wrong working directory / sys.path

**Symptom.** Your own package imports fail from inside a script but work
from the repo root. `python foo/bar.py` fails but `python -m foo.bar`
works.

**Deeper diagnostics.**

- `python -c "import sys; print('\n'.join(sys.path))"` — the CWD isn't
  automatically on sys.path when you run a script by path (it IS when
  you use `-m`).
- Editable install: `pip install -e .` resolves most of these cases by
  registering your package via an egg-link.
- `PYTHONPATH` env var prepends entries to sys.path — useful for
  one-off scripts, but a symptom of a missing install if needed in
  production.

## D. Circular imports

**Symptom.** `ImportError: cannot import name 'X' from partially
initialized module 'Y'` or similar. The traceback shows one module
started importing, pulled in another, which reached back to the first
before it finished initialising.

**Deeper diagnostics.**

1. **Identify the cycle.** Read the traceback top-down: `A` imports
   `B` imports `A` (or a longer chain). Often `A.py` does
   `from B import thing` at module top-level, while `B.py` does
   `from A import otherthing` at its own top-level.
2. **Break it.** Three standard fixes, in order of preference:
   - Extract the shared symbol into a third module `C` that both
     `A` and `B` import. This is the cleanest split.
   - Convert one direction of the import to a *local* import inside
     a function, so the import runs at call time (past the init
     phase) rather than at module load time.
   - Use `typing.TYPE_CHECKING` for type-only imports — these run at
     type-check time only, not at runtime.

**Warning signs.** Two top-level modules that need each other's names
are usually a design smell. The extracted-third-module fix usually
makes the architecture better, not just the imports.
