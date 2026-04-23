---
name: Test runner
description: Use when the user asks to run the tests, run pytest, check if tests pass, run the test suite, run unit tests, verify the build, make sure nothing is broken, or check for regressions.
version: 0.1.0
---

# Test Runner

Run the project's test suite and interpret the output.

## 1. Detect the runner

Look at project markers:
- `pyproject.toml` / `pytest.ini` / `setup.cfg` → **pytest**
- `vitest.config.ts` / `vitest.config.js` → **vitest**
- `jest.config.ts` / `jest.config.js` → **jest**
- `Cargo.toml` → **cargo test**
- `go.mod` → **go test ./...**

The bundled `RunTests` tool already does this detection; prefer that over shelling out manually.

## 2. Narrow the scope first

Before running everything, ask:
- Are there failing tests already? (`git status`, check CI status)
- Is there a specific file / function the user is asking about?
- Run the narrowest matching set first (e.g. `pytest tests/test_auth.py -v`) before the full suite.

## 3. Report failures concisely

For each failure:
- Test name + file:line
- One-line "what the test expects" vs "what it got"
- Don't paste the full traceback unless the user asks — summarize first.

## 4. Distinguish flakes from real failures

Before declaring a test broken:
- Did the test pass on `main`? Run `git stash && pytest && git stash pop` if unsure.
- Is it timing / network dependent? Retry once with `-x --tb=short`.
- Is the failure in the code under test, the test itself, or test infrastructure?

## 5. Offer next action

After reporting results:
- All green → "tests pass, ready to commit"
- Real failure → propose a minimal reproduction or fix location
- Unclear → ask for more context, don't guess
