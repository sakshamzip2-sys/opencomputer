# F7 — Coding-Harness Interweaving Plan (Session A's Phase 5 Refactor Contract)

> **Audience: Session A's Phase 5 implementer.** Session C ships `extensions/oi-capability/` as a **standalone plugin** in C3. The master plan (`declarative-moseying-glade.md` §F7) requires F7 to be "interwoven with coding-harness, not standalone." This document is the contract for how Session A refactors C3's standalone plugin into the coding-harness bridge.
>
> Reading this contract should let Session A's Phase 5 do the refactor mechanically (no rewrite, no re-design).

---

## 1. Why this exists

The master plan's F7 requirement was "OI tools as a coding-harness bridge layer," but the parallel-session protocol forbids Session C (and Session B) from touching `extensions/coding-harness/*` (Session A's reserved territory).

**Resolution:** Session C ships `extensions/oi-capability/` standalone in C3 (zero coding-harness modification). Session A's Phase 5 refactors it into `extensions/coding-harness/oi_bridge/`. This document specifies the refactor exactly so Session A doesn't have to re-design.

---

## 2. Source layout (C3 ships)

```
extensions/oi-capability/
├── plugin.py                       ← register stub (no tool registration)
├── plugin.json                     ← enabled_by_default: false
├── subprocess/
│   ├── __init__.py
│   ├── server.py                   ← OI subprocess JSON-RPC dispatcher
│   ├── telemetry_disable.py        ← pre-import telemetry no-op
│   ├── venv_bootstrap.py           ← lazy venv creation
│   ├── wrapper.py                  ← parent-side process management + JSON-RPC client
│   └── protocol.py                 ← request/response schemas + error codes
└── tools/
    ├── __init__.py
    ├── tier_1_introspection.py     ← 8 tools
    ├── tier_2_communication.py     ← 5 tools
    ├── tier_3_browser.py           ← 3 tools
    ├── tier_4_system_control.py    ← 4 tools
    └── tier_5_advanced.py          ← 3 tools
```

---

## 3. Target layout (Session A's Phase 5 refactor)

```
extensions/coding-harness/
├── plugin.py                       ← MODIFIED — wires oi_bridge tools alongside coding tools
├── ... (existing coding-harness files unchanged)
└── oi_bridge/                      ← NEW — moved from oi-capability/
    ├── __init__.py
    ├── subprocess/                 ← MOVED verbatim — no edits to subprocess/* files
    │   ├── __init__.py
    │   ├── server.py
    │   ├── telemetry_disable.py
    │   ├── venv_bootstrap.py
    │   ├── wrapper.py
    │   └── protocol.py
    └── tools/                      ← MOVED + small wiring edits
        ├── __init__.py
        ├── tier_1_introspection.py  ← UNCHANGED logic; add ConsentGate.require + AuditLog calls
        ├── tier_2_communication.py  ← same pattern
        ├── tier_3_browser.py        ← same pattern
        ├── tier_4_system_control.py ← + SandboxStrategy.guard calls
        └── tier_5_advanced.py       ← same pattern as tier 4
```

The `extensions/oi-capability/` directory is **DELETED** as part of the refactor — the standalone plugin existence is provisional, intended only for the C3-to-Phase-5 gap.

---

## 4. Refactor steps (mechanical, in order)

### Step 1: Move files (no edits)
```bash
git mv extensions/oi-capability/subprocess/ extensions/coding-harness/oi_bridge/subprocess/
git mv extensions/oi-capability/tools/ extensions/coding-harness/oi_bridge/tools/
```

### Step 2: Update imports
The `from extensions.oi_capability.subprocess.X import Y` lines in `tools/*.py` become `from extensions.coding_harness.oi_bridge.subprocess.X import Y`. Mechanical sed.

### Step 3: Wire ConsentGate + SandboxStrategy + AuditLog into each tool's `execute()`

Each tool's `execute()` method has clearly marked extension points (per C3 design):

```python
# C3 ships:
async def execute(self, **kwargs):
    # CONSENT_HOOK — wire ConsentGate.require here in Phase 5
    # SANDBOX_HOOK — wire SandboxStrategy.guard here in Phase 5 (Tier 4-5 only)
    result = await self._wrapper.call("computer.X.Y", kwargs)
    # AUDIT_HOOK — wire AuditLog.append here in Phase 5
    return result
```

Phase 5 replaces the comment markers with real calls. Mechanical, line-by-line.

### Step 4: Register tools in `coding-harness/plugin.py`
The `register(api)` function in `coding-harness/plugin.py` adds:
```python
from extensions.coding_harness.oi_bridge.tools import (
    tier_1_introspection, tier_2_communication,
    tier_3_browser, tier_4_system_control, tier_5_advanced,
)
for module in (tier_1_introspection, tier_2_communication,
               tier_3_browser, tier_4_system_control, tier_5_advanced):
    for tool_cls in module.ALL_TOOLS:
        if config.oi_capability.is_tool_enabled(tool_cls.name):
            api.register_tool(tool_cls(consent_gate=consent_gate,
                                       sandbox=sandbox,
                                       audit=audit_log,
                                       wrapper=oi_wrapper))
```

### Step 5: Move tests
```bash
git mv tests/test_oi_*.py tests/test_coding_harness_oi_*.py
```
Update test imports to match new module paths. Mechanical sed.

### Step 6: Delete the standalone plugin
```bash
git rm -rf extensions/oi-capability/
```

### Step 7: Update plugin manifests
- `extensions/coding-harness/plugin.json` — bump version, add a manifest field describing the OI bridge inclusion (so users see it in `opencomputer plugins`).
- The previously-existing `extensions/oi-capability/plugin.json` is gone (deleted in Step 6).

### Step 8: Documentation update
- `docs/f7/README.md` — update phase status, install instructions, adjust paths. The plugin is now `coding-harness`, not `oi-capability`.
- `docs/f7/design.md` — add a §16 "Phase 5 refactor complete — folded into coding-harness on YYYY-MM-DD"
- `docs/parallel-sessions.md` — Session C's reserved-files list updates (oi-capability gone).

---

## 5. Why this refactor is mechanical

Three load-bearing design choices in C3 make the Phase 5 refactor trivial:

1. **No cross-module dependencies between `oi-capability/` and `opencomputer/*`.** The plugin imports only from `plugin_sdk/*` and from its own subdirectories. Moving the dir doesn't break imports outside.
2. **Extension points are pre-declared.** `# CONSENT_HOOK` / `# SANDBOX_HOOK` / `# AUDIT_HOOK` markers in every tool — Phase 5 doesn't have to discover where to wire; the spots are labeled.
3. **Tools are class-based with constructor injection.** `ToolClass(consent_gate=..., sandbox=..., audit=...)` is the constructor signature. Phase 5 just passes the real instances; no changes to tool internals.

---

## 6. What Phase 5 must NOT do

- **Do not rewrite the subprocess server.** It works as-is. The wire protocol is locked.
- **Do not change the JSON-RPC error codes.** External consumers (B5+ evolution metrics?) may depend on them.
- **Do not change the per-tool consent-tier numbers.** They're load-bearing for the consent-prompt language template (`design.md` §6).

---

## 7. Coordination commitments

- **Session A**: confirms this plan in C1 PR review. If concerns, raises BEFORE C3 starts so we can adjust.
- **Session C**: ships C3 with the extension points exactly as named here. If implementation surprises change a name, updates this doc.
- **Both sessions**: log the eventual refactor commit in `docs/parallel-sessions.md` so traceability holds.

---

## 8. Failure mode: what if Phase 5 refactor never happens?

If Session A's Phase 5 is delayed indefinitely, `extensions/oi-capability/` (standalone) becomes the permanent surface. That's acceptable but suboptimal — the master plan's "interwoven" requirement isn't met.

**Mitigation**: in C3 PR review, Session A explicitly confirms commitment to do the refactor in Phase 5. If commitment isn't given, C3 ships anyway as standalone (we already have user value); we then revisit later.

---

*This document is a contract between C3 (Session C) and Phase 5 (Session A). Last updated: C1 landing.*
