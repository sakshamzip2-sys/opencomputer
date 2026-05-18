# Dashboard `auth_status` Label Relabel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the misleading `"unused"` value of the dashboard's plugin `auth_status` field with `"none"` (meaning "no auth required"), so users stop reading it as "this plugin isn't doing anything."

**Architecture:** One enum value rename across three surfaces — backend Python (plugin_api.py), frontend static HTML (plugins.html), pinning test (test_dashboard_fastapi.py). Backwards-compat alias preserved server-side so the SPA's `oc-workspace/electron/server-bundle.cjs` (built artifact) and any external API client that grew to depend on `"unused"` keeps working through one release.

**Tech Stack:** FastAPI (backend), vanilla JS + CSS pills (frontend static), pytest (tests).

---

## Background

The dashboard's plugin table at `/api/plugins/management/list` returns one row per discovered plugin with an `auth_status` field. The field answers: "Does this plugin's manifest declare env vars I need to populate before it'll work?"

Three values:
- `"configured"` — env vars declared AND all present → green pill
- `"missing"` — env vars declared AND at least one absent → yellow pill
- `"unused"` — no env vars declared → "n/a"-style gray pill

The `"unused"` label is mis-read by users as "this plugin is unused" (i.e. inactive / not doing anything). The user saw a list of plugins all marked `unused` and concluded the dashboard was telling them every plugin was idle. The actual meaning is "no auth required for this plugin to work."

**Fix:** rename `"unused"` → `"none"` (alongside the existing `"configured"` / `"missing"`). `"none"` reads honestly as "no auth required" — same semantic, clearer label. The HTML pill rendering uses the same `na` styling.

**Backwards compat:** the `/list` endpoint MUST accept the rename without breaking the static HTML frontend (we control that), without breaking the `oc-workspace` built bundle (built artifact, we don't regen here), and without breaking any third-party consumer that grew to read `auth_status`. Strategy: emit both `auth_status: "none"` (new canonical) AND `auth_status_legacy: "unused"` (deprecation alias) for one release. Mark the legacy field with a deprecation comment; remove in a follow-up PR after the bundle is rebuilt.

## File Structure

- **Modify**: `opencomputer/dashboard/plugins/management/plugin_api.py` — change `_provider_auth_status()` return string from `"unused"` to `"none"`, add backwards-compat alias field to the dict the `/list` endpoint emits.
- **Modify**: `opencomputer/dashboard/static/plugins.html` — update the JS that maps `auth_status` → CSS pill kind: accept both `"none"` (new) and `"unused"` (legacy) as `na`, accept `"missing"` as `warn`, accept `"configured"` as `ok`. Forward-compat + back-compat.
- **Modify**: `tests/test_dashboard_fastapi.py` — update the pinning assertion that lists the 3 valid values: `("configured", "missing", "none")`. Add a second assertion that `auth_status_legacy` matches `auth_status` when it's `"none"` (legacy emitted only for back-compat). Add a third assertion that for a plugin with env vars, `auth_status` is NOT `"none"`.
- **Modify**: `tests/test_dashboard_plugins_management_api.py` — IF tests exist for this module (will check during execution; if absent, skip).

## Tasks

### Task 1: Add the failing test — pin the new enum values

**Files:**
- Modify: `tests/test_dashboard_fastapi.py:73` — update the inline tuple of valid values to include `"none"` and assert `auth_status_legacy` exists.

- [ ] **Step 1: Read the current test to see exact context**

```bash
sed -n '60,90p' tests/test_dashboard_fastapi.py
```

Expected: see `assert sample["auth_status"] in ("configured", "missing", "unused")` around line 73.

- [ ] **Step 2: Replace the assertion with the new tuple**

Find the line:

```python
assert sample["auth_status"] in ("configured", "missing", "unused")
```

Replace with:

```python
# auth_status canonical values are configured/missing/none.
# auth_status_legacy carries the old "unused" alias for one release
# while the oc-workspace built bundle still expects it; remove in a
# follow-up PR once the bundle is rebuilt.
assert sample["auth_status"] in ("configured", "missing", "none")
assert "auth_status_legacy" in sample
if sample["auth_status"] == "none":
    assert sample["auth_status_legacy"] == "unused"
else:
    assert sample["auth_status_legacy"] == sample["auth_status"]
```

- [ ] **Step 3: Run the test to verify it fails**

Run:

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_dashboard_fastapi.py -k "list" --tb=short
```

Expected: FAIL — `assert "missing_or_unused" in ("configured", "missing", "none")` etc. Some variant of "value not in tuple" or "key auth_status_legacy not present". Exact failure depends on which assertion runs first against the unchanged backend.

- [ ] **Step 4: Commit the failing test**

```bash
git add tests/test_dashboard_fastapi.py
git commit -m "test: pin auth_status enum to (configured, missing, none) + legacy alias"
```

### Task 2: Implement the backend — emit `"none"` + `auth_status_legacy: "unused"`

**Files:**
- Modify: `opencomputer/dashboard/plugins/management/plugin_api.py:63-75` — the `_provider_auth_status()` helper returns `"none"` (canonical). 
- Modify: `opencomputer/dashboard/plugins/management/plugin_api.py:121-131` — the dict built for each plugin row in `list_plugins()` adds an `auth_status_legacy` field.

- [ ] **Step 1: Read the current helper + emit site**

```bash
sed -n '63,75p' opencomputer/dashboard/plugins/management/plugin_api.py
sed -n '121,132p' opencomputer/dashboard/plugins/management/plugin_api.py
```

Expected: see `return "unused"` at line 71 and the dict-construction at line 121-131.

- [ ] **Step 2: Update `_provider_auth_status()` to return `"none"` instead of `"unused"`**

Find:

```python
def _provider_auth_status(env_vars: tuple[str, ...]) -> str:
    """Return one of ``configured``/``missing``/``unused``.

    ``configured`` — every declared env var is set (non-empty)
    ``missing``    — at least one declared env var is unset/empty
    ``unused``     — manifest declares no env vars (no auth needed)
    """
    if not env_vars:
        return "unused"
    for name in env_vars:
        if not os.environ.get(name):
            return "missing"
    return "configured"
```

Replace with:

```python
def _provider_auth_status(env_vars: tuple[str, ...]) -> str:
    """Return one of ``configured``/``missing``/``none``.

    ``configured`` — every declared env var is set (non-empty)
    ``missing``    — at least one declared env var is unset/empty
    ``none``       — manifest declares no env vars (no auth required)

    Renamed from ``unused`` (which read as "this plugin is unused") to
    ``none`` (which reads as "no auth required"). The legacy ``unused``
    value is still emitted alongside this one in the ``auth_status_legacy``
    field for one release; the oc-workspace built bundle and any external
    API client gets a deprecation window before we remove the alias.
    """
    if not env_vars:
        return "none"
    for name in env_vars:
        if not os.environ.get(name):
            return "missing"
    return "configured"


def _legacy_auth_status(canonical: str) -> str:
    """Map the canonical auth_status to its legacy string for back-compat.

    Same value for configured/missing; ``none`` maps back to the old
    ``unused`` string so consumers that haven't migrated still parse.
    Remove once oc-workspace/electron/server-bundle.cjs has been
    regenerated and any third-party consumers have migrated.
    """
    return "unused" if canonical == "none" else canonical
```

- [ ] **Step 3: Update the docstring of `list_plugins()` to reflect the new value**

Find at line 94:

```python
              "auth_status": "configured" | "missing" | "unused",
```

Replace with:

```python
              "auth_status": "configured" | "missing" | "none",
              "auth_status_legacy": "configured" | "missing" | "unused",  # deprecated; one-release alias
```

- [ ] **Step 4: Emit `auth_status_legacy` in the per-row dict**

Find at line 121-131:

```python
        plugins_out.append({
            "id": m.id,
            "name": m.name,
            "version": getattr(m, "version", "0.0.0"),
            "kind": getattr(m, "kind", ""),
            "description": getattr(m, "description", ""),
            "enabled": is_enabled,
            "auth_status": _provider_auth_status(env_vars),
            "env_vars": list(env_vars),
            "source_root": str(cand.root_dir),
        })
```

Replace with:

```python
        auth_status = _provider_auth_status(env_vars)
        plugins_out.append({
            "id": m.id,
            "name": m.name,
            "version": getattr(m, "version", "0.0.0"),
            "kind": getattr(m, "kind", ""),
            "description": getattr(m, "description", ""),
            "enabled": is_enabled,
            "auth_status": auth_status,
            # Deprecated alias — remove after oc-workspace bundle rebuilt
            # and any external API consumer has migrated. See _legacy_auth_status.
            "auth_status_legacy": _legacy_auth_status(auth_status),
            "env_vars": list(env_vars),
            "source_root": str(cand.root_dir),
        })
```

- [ ] **Step 5: Run the test to verify it passes**

Run:

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_dashboard_fastapi.py -k "list" --tb=short
```

Expected: PASS — the canonical assertion accepts `"none"`, the legacy field exists and maps to `"unused"` when canonical is `"none"`.

- [ ] **Step 6: Run broader plugin_api tests to confirm no collateral break**

Run:

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/ -k "plugin_api or dashboard_plugin or dashboard_fastapi" --tb=line -q
```

Expected: all 28+ tests pass (baseline was 28 passed + 6 skipped).

- [ ] **Step 7: Commit the backend change**

```bash
git add opencomputer/dashboard/plugins/management/plugin_api.py
git commit -m "feat(dashboard): rename auth_status 'unused' -> 'none' with legacy alias

The 'unused' label was being read by users as 'this plugin is unused
or idle'. The actual meaning is 'no auth required for this plugin to
work'. Rename to 'none' which reads honestly.

Emits both auth_status (canonical: configured/missing/none) and
auth_status_legacy (one-release alias: configured/missing/unused) so
the oc-workspace built bundle and any external API consumer gets a
deprecation window before the legacy field is removed."
```

### Task 3: Update the frontend — accept both new and legacy values

**Files:**
- Modify: `opencomputer/dashboard/static/plugins.html:154-155` — the JS that maps `auth_status` to CSS pill kind.

- [ ] **Step 1: Read the current frontend mapping**

```bash
sed -n '150,170p' opencomputer/dashboard/static/plugins.html
```

Expected: see the ternary `p.auth_status === 'configured' ? 'ok' : p.auth_status === 'missing' ? 'warn' : 'na'`.

- [ ] **Step 2: Update the ternary to be explicit about the new value**

The current code already falls through to `'na'` for any non-configured/non-missing value, so functionally `"none"` and `"unused"` both get `'na'`. But the user-visible pill text comes from `p.auth_status` directly via `OCDash.statusPill(p.auth_status, authKind)` at line 164. We want the pill to show `"none"` going forward, NOT `"unused"`. The backend already emits `"none"` now (canonical), so the rendered text changes automatically once the backend ships. No frontend code change is strictly required, BUT we should update the comment + add an explicit branch so the intent is documented.

Find:

```javascript
          const authKind = p.auth_status === 'configured' ? 'ok' :
                           p.auth_status === 'missing' ? 'warn' : 'na';
```

Replace with:

```javascript
          // auth_status canonical: 'configured' | 'missing' | 'none'.
          // 'unused' is the legacy value retained server-side as
          // auth_status_legacy for one release; treat it same as 'none'.
          const authKind = p.auth_status === 'configured' ? 'ok' :
                           p.auth_status === 'missing' ? 'warn' :
                           (p.auth_status === 'none' || p.auth_status === 'unused') ? 'na' :
                           'na';
```

- [ ] **Step 3: Smoke-test the frontend mentally**

The change is comment + explicit branch. Static HTML — no test runner. Visual verification deferred to manual testing in Task 4.

- [ ] **Step 4: Commit the frontend change**

```bash
git add opencomputer/dashboard/static/plugins.html
git commit -m "fix(dashboard): plugins.html — document auth_status 'none' canonical + accept legacy 'unused'"
```

### Task 4: Verify end-to-end via curl probe

**Files:**
- Read-only verification: hit `/api/plugins/management/list` against a running dashboard and check the response shape.

- [ ] **Step 1: Find an existing dashboard test that already spins up the FastAPI app**

```bash
grep -l "TestClient\|fastapi.testclient" tests/test_dashboard_fastapi.py tests/test_dashboard_plugins_management*.py 2>/dev/null
```

Expected: `tests/test_dashboard_fastapi.py` uses `TestClient`.

- [ ] **Step 2: Add a focused end-to-end test asserting the response shape**

Append to `tests/test_dashboard_fastapi.py`:

```python
def test_auth_status_uses_none_not_unused(client):
    """Regression: the canonical auth_status value is 'none', not the
    old misleading 'unused'. Surfaces if anyone reverts the rename."""
    resp = client.get("/api/plugins/management/list")
    assert resp.status_code == 200
    payload = resp.json()
    statuses = {row["auth_status"] for row in payload["plugins"]}
    # At least one plugin in the discovery set has no env vars (e.g. the
    # bundled coding-harness which works without auth). Confirms 'none'
    # actually emits and 'unused' has been removed from the canonical
    # value set.
    assert "none" in statuses or len(statuses) == 0, (
        f"Expected at least one plugin with auth_status='none' for "
        f"plugins without env vars; got {statuses}"
    )
    assert "unused" not in statuses, (
        f"auth_status 'unused' was renamed to 'none'; got {statuses}. "
        f"This value should only appear in auth_status_legacy now."
    )
```

NOTE: this test depends on the `client` fixture used by the existing `test_dashboard_fastapi.py`. Confirm the fixture name by reading the test file's existing fixture references at step 1 before writing this test — if the fixture is named differently (e.g. `test_client`, `app_client`), use that name. If no fixture exists, fall back to creating a local one inline:

```python
def test_auth_status_uses_none_not_unused(tmp_path, monkeypatch):
    """..."""
    from fastapi.testclient import TestClient
    from opencomputer.dashboard.server import create_app
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/plugins/management/list")
    # ... rest unchanged
```

- [ ] **Step 3: Run the new regression test**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/test_dashboard_fastapi.py::test_auth_status_uses_none_not_unused -xvs
```

Expected: PASS.

- [ ] **Step 4: Run the full dashboard-plugin test suite to confirm no regression**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/ -k "plugin_api or dashboard_plugin or dashboard_fastapi" --tb=line -q
```

Expected: ≥ 28 passed (matching baseline). New regression test adds 1 → expected ≥ 29 passed.

- [ ] **Step 5: Commit the regression test**

```bash
git add tests/test_dashboard_fastapi.py
git commit -m "test(dashboard): regression test pinning auth_status='none' not 'unused'"
```

### Task 5: Lint pass

**Files:**
- Read-only verification.

- [ ] **Step 1: Run ruff on the changed Python files**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m ruff check opencomputer/dashboard/plugins/management/plugin_api.py tests/test_dashboard_fastapi.py
```

Expected: `All checks passed!`

- [ ] **Step 2: If ruff complains, fix and re-run**

If unused imports or formatting violations appear, fix inline (do not split into a separate commit unless it's a non-trivial cleanup).

### Task 6: Final test suite sweep

**Files:**
- Read-only verification.

- [ ] **Step 1: Run the broader test suite to confirm no unrelated break**

```bash
/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python -m pytest tests/ -k "dashboard" --tb=line -q 2>&1 | tail -20
```

Expected: a count similar to or matching the pre-change baseline. Any failure unrelated to `auth_status` must be flagged but is NOT this PR's job.

- [ ] **Step 2: If green, proceed to finishing-a-development-branch**

If failures appear that ARE related to `auth_status` (e.g. another test pinning the old value), surface them — they need to be either updated to `"none"` or removed (if redundant with the regression test).

## Self-review checklist (run before handoff to executor)

1. **Spec coverage**: every behavior in "Background" is covered by a task. Backend rename ✓ (Task 2). Frontend update ✓ (Task 3). Pinning test ✓ (Task 1). Regression test ✓ (Task 4). Legacy alias ✓ (Task 2 step 4).

2. **Placeholder scan**: every step has concrete code blocks. No "TBD" / "appropriate" / "similar to" anywhere. ✓

3. **Type consistency**: `auth_status` is `str` everywhere. `_legacy_auth_status` is a new pure function returning `str`. No type drift. ✓

4. **Back-compat hazard**: `oc-workspace/electron/server-bundle.cjs` is a built artifact NOT updated by this PR. The `auth_status_legacy` field is the bridge. Documented in commit messages. ✓

5. **Test isolation**: the regression test (Task 4) doesn't depend on which plugins are installed on disk — it asserts `"unused" not in statuses` (universally true) and `"none" in statuses OR no plugins discovered` (handles empty-discovery edge case). ✓

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-18-dashboard-auth-status-relabel.md`.

Executing inline via `superpowers:executing-plans` per the user's request — no subagent dispatch.
