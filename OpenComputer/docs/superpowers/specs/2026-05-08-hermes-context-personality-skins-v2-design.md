# Hermes Context/Personality/Skins v2 — Parity Gap-Fill Design

**Date:** 2026-05-08
**Source spec:** `~/Downloads/files (1)/hermes-context-personality-skins-v2.md`
**Status:** Approved for execution under `/effort max` + `auto` mode.

---

## Problem

The v2 reference describes Hermes' end-state for four subsystems:

1. Context-file loading (`.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` / `SOUL.md`)
2. `@`-references (`@file:`, `@folder:`, `@diff`, `@staged`, `@git:N`, `@url:`)
3. Personalities (14 built-ins + custom + `/personality`)
4. Skins / themes (9 built-ins + custom + `/skin`)

Most of this has already shipped in OpenComputer (notably PR #500 + Sub-project C / PR #24). The remaining work is the **concrete delta** between the v2 spec and what's on `main` today.

## In-scope deltas (all surface area)

| ID | Gap | Severity | Action |
|---|---|---|---|
| **A** | `subdirectory_hints._scan_context_content` is a documented no-op stub. Subdirectory-injected context bypasses the prompt-injection scan + secret redaction that startup loading already enforces. | 🔴 security | Refactor `prompt_builder._post_process_workspace_context` into a shared helper (`opencomputer.security.context_scan.scan_workspace_context_content`) and call it from both startup loader and subdir hint tracker. |
| **B** | `.cursorrules` is not in the startup loader's `target_names` tuple — only `OPENCOMPUTER.md`/`CLAUDE.md`/`AGENTS.md`. Subdirectory hints already scan `.cursorrules`, so the inconsistency only hits the cwd at startup. | 🟡 functional | Append `.cursorrules` to `prompt_builder.load_workspace_context.target_names` (last-priority slot, after `AGENTS.md`). |
| **C** | Truncation marker is generic: `[truncated — file exceeded 100KB cap]`. Hermes spec gives kept-counts + a hint to use file tools so the agent can recover the rest. | 🟢 polish | New marker format: `[...truncated NAME: kept Nh+Nt of N chars. Use file tools to read the full file.]` Updates one test (`test_prompt_builder_redaction.py:64`). |
| **F** | No parity-status reference doc. Future contributors hitting this v2 reference need a one-page map of what's already in OC. | 🟢 docs | Write `docs/refs/hermes-context-personality-skins-v2-parity.md` with the inventory table from the brainstorm. |

## Out-of-scope (YAGNI cuts, called out for posterity)

- `waiting_faces` / `thinking_faces` SkinSpec fields — no current renderer site.
- Expanding `default.yaml` to all 24 Hermes color keys — current 15 cover OC's render surfaces; rest are decorative.
- "Hermes Mod" web UI — community tool, not a port target.
- Single-global `HERMES_HOME/SOUL.md` — OC uses per-profile by design (Sub-project C, PR #24). Not a gap.
- `.hermes.md` priority — OC uses `OPENCOMPUTER.md`. Not a gap.
- Tab completion for `@`-syntax — already provided by the prompt-toolkit input loop.

## Architecture

### A. Shared workspace-context scanner

**Before:**
```
prompt_builder.load_workspace_context() → _post_process_workspace_context() → scanned

subdirectory_hints._load_hints_for_directory() → _scan_context_content()  ← no-op stub
```

**After:**
```
opencomputer/security/context_scan.py
  └─ scan_workspace_context_content(raw, source) → (text, scrubbed)

prompt_builder._post_process_workspace_context() → calls shared helper
subdirectory_hints._scan_context_content()    → calls shared helper
```

The shared helper does, in order:

1. `redact_runtime_text_with_counts` — strips secrets + PII before the LLM ever sees them.
2. `default_detector().detect()` — returns a `Verdict` with `quarantine_recommended` + `triggered_rules` + `confidence`.
3. If quarantine recommended → wrap in `<quarantined-untrusted-content>` with an HTML comment naming the rules + confidence.

This matches the existing startup behavior verbatim. Renaming-only refactor — no policy change for startup; new policy for subdir hints (which now actually run the scan).

### B. `.cursorrules` startup priority

Single-line append:
```python
target_names = ("OPENCOMPUTER.md", "CLAUDE.md", "AGENTS.md", ".cursorrules")
```

`.cursorrules` is plain-text; the existing read loop handles it. The hint label in the assembled output uses the literal filename, so users see `## .cursorrules` consistently with the other entries.

### C. Truncation marker

**Before:** `\n\n[truncated — file exceeded 100KB cap]\n`

**After:** `\n\n[...truncated NAME: kept Nh chars of N total. Use file tools to read the full file.]\n`

Where `Nh = 100_000` and `N = len(content)` at the call site. We *don't* implement Hermes' 70%/20%/10% head/tail/marker split — that's a different truncation strategy (mid-file marker), not just a different message. Sticking with head-only truncation (current behavior) keeps the line-number invariants stable for downstream tools. The user-visible improvement is that the marker tells the agent **what to do next**.

### F. Parity-status doc

`docs/refs/hermes-context-personality-skins-v2-parity.md` — single page table mapping each v2 spec section to (a) the OC module that implements it, (b) status (✅ shipped / ⚠️ partial / ❌ not applicable), and (c) any deviations (e.g. SOUL.md per-profile).

## Failure modes

| Risk | Mitigation |
|---|---|
| Shared scanner crashes on subdir hint | `subdirectory_hints._load_hints_for_directory` already swallows `Exception`; we log + return content unchanged. Fail-open — same posture as startup. |
| Quarantine envelope double-wraps if subdir hint contains a quarantined fragment from upstream | Detector pattern set doesn't match its own envelope strings; one-shot scan is safe. |
| `.cursorrules` 5MB file | Existing `_WORKSPACE_FILE_CAP_BYTES = 100_000` truncates before scan. |
| Existing test `test_prompt_builder_redaction.py:64` asserts old marker text | Update assertion to new marker format. |
| New shared helper breaks public import surface | Helper is private (`opencomputer.security.context_scan`); export gated under `__all__`. No external consumers today. |

## Test plan

1. New: `tests/test_subdirectory_hints_security.py`
   - `.cursorrules` in subdir with `ignore previous instructions` triggers quarantine envelope in returned hint string.
   - Subdir hint with `OPENAI_API_KEY=sk-...` redacts the secret before injection.
   - Clean subdir content passes through unchanged.
2. New: `tests/test_context_scan_shared.py`
   - Helper returns `(scrubbed, was_quarantined)` correctly for clean / poisoned / secret-bearing inputs.
3. Update: `tests/agent/test_prompt_builder_redaction.py:64`
   - New marker text.
4. New: `tests/test_workspace_context_cursorrules.py`
   - `.cursorrules` at start dir is loaded and labeled with `## .cursorrules`.
5. Existing: full `tests/test_subdirectory_hints.py` (15-ish tests) must stay green.
6. Existing: full `tests/test_workspace_context.py` (10-ish tests) must stay green.

## Effort estimate

- A: ~30 LOC + 4 tests + 1 helper module = 30 min implementation + 30 min tests
- B: ~5 LOC + 1 test = 10 min total
- C: ~10 LOC + 1 test update = 15 min total
- F: 1 doc page = 30 min
- Plumbing (branch / commit / PR) = 15 min

**Total: ~2 hours.**
