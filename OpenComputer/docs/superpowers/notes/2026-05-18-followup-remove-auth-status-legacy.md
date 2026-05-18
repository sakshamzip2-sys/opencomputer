# Follow-up: remove `auth_status_legacy` from the plugins-management API

**Filed:** 2026-05-18
**Triggers (any one removes the alias):**

1. `oc-workspace/electron/server-bundle.cjs` is rebuilt from source AFTER 2026-06-18.
   - Verify by checking the bundle's git diff for new build output.
2. A reachable external API consumer is identified using `auth_status_legacy` — that consumer is migrated to `auth_status` canonical, THEN the alias is removed.
3. **Hard deadline: 2026-08-18.** Three months from introduction. Whether or not anyone has migrated, the alias goes away on this date — its value is "give consumers a heads-up," and three months is enough.

## What to remove

Backend (`opencomputer/dashboard/plugins/management/plugin_api.py`):

```python
# DELETE this function entirely
def _legacy_auth_status(canonical: str) -> str:
    ...

# DELETE this field from the per-plugin dict
"auth_status_legacy": _legacy_auth_status(auth_status),

# DELETE this docstring line
"auth_status_legacy": "configured" | "missing" | "unused",  # deprecated; one-release alias

# DELETE these phrases from the helper's docstring
The legacy ``unused`` value is still emitted alongside this one in
the ``auth_status_legacy`` field for one release; ...
```

Frontend (`opencomputer/dashboard/static/plugins.html`):

```javascript
// SIMPLIFY this branch back to the original ternary — drop the 'unused' fallback
const authKind = p.auth_status === 'configured' ? 'ok' :
                 p.auth_status === 'missing' ? 'warn' : 'na';

// DELETE the multi-line comment above it referencing the legacy alias
```

Tests (`tests/test_dashboard_fastapi.py`):

```python
# DELETE these assertions from test_management_plugin_lists_plugins
assert "auth_status_legacy" in sample
if sample["auth_status"] == "none":
    assert sample["auth_status_legacy"] == "unused"
else:
    assert sample["auth_status_legacy"] == sample["auth_status"]

# DELETE the auth_status_legacy checks in test_auth_status_uses_none_not_unused
for row in payload["plugins"]:
    assert "auth_status_legacy" in row, ...
    if row["auth_status"] == "none":
        assert row["auth_status_legacy"] == "unused"
    else:
        assert row["auth_status_legacy"] == row["auth_status"]
```

## Verification

After the cleanup PR:

```bash
# 1. Confirm no remaining references
grep -rn auth_status_legacy opencomputer/ tests/
# Expected: zero matches

grep -rn '"unused"' opencomputer/dashboard/
# Expected: zero matches (the alias was the only remaining reference)

# 2. Test the canonical path still works
.venv/bin/python -m pytest tests/test_dashboard_fastapi.py -k "auth_status" -v
# Expected: PASS

# 3. Visual smoke: open the dashboard plugins page
oc dashboard
# Expected: pill text shows "none" (not "unused") for plugins without env vars
```

## Why not remove now

The OpenAPI shape `/api/plugins/management/list` is consumed by:

1. `opencomputer/dashboard/static/plugins.html` — controlled by us, updated in the same PR as the backend rename
2. `oc-workspace/electron/server-bundle.cjs` — verified at 2026-05-18 to NOT reference `auth_status` at all, so this consumer is N/A
3. Third-party API consumers — no audit list exists; the 3-month deprecation window is the conservative answer

If at the 3-month review point (2) is still N/A and (3) has no evidence of being a real concern, the alias should have been removed sooner. Next time a similar rename is needed, default to "no alias, one PR" unless a specific external consumer is identified.

## Original PR

`feat(dashboard): rename auth_status 'unused' -> 'none' with legacy alias` — commit `94b9a9ca` on branch `feat/dashboard-auth-status-relabel-2026-05-18`.
