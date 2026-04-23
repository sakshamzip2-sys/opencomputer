---
name: Refactorer
description: Use when the user asks to refactor, clean up, simplify, extract a function, rename, reorganize, split up, consolidate, or improve the structure of existing code without changing behavior.
version: 0.1.0
---

# Refactorer

Structural change without behavior change. The critical rule: **every step is reversible and tested.**

## 1. Understand the current shape first

- Read the file(s) involved end-to-end before proposing any change.
- Identify the tests that cover the code. If none, refactoring is risky — offer to add tests first.
- Note the public API boundary: what callers depend on?

## 2. Pick a refactoring style

- **Extract**: pull a helper out of a long function.
- **Rename**: clarify naming. Use Grep to find every call site first.
- **Split**: break a large file into smaller files by concern. Use the harness's `/checkpoint` before you start so /undo works.
- **Inline**: collapse an unnecessarily indirect helper.
- **Reorganize**: move things between files/modules. Update imports.

Don't mix styles in one refactor. One kind at a time.

## 3. Execute in small steps

For each change:
1. `/checkpoint pre-<step-name>` — snapshot state so the user can `/undo`.
2. Make the smallest possible edit.
3. Run the relevant tests. Do not touch the next step until this one is green.
4. If tests go red, `/undo` and try a different approach.

## 4. Don't add features, don't remove features

A good refactor:
- Does NOT introduce new parameters, handlers, logic branches.
- Does NOT remove existing parameters, handlers, logic branches.
- DOES keep exported names + signatures + behavior identical.

If you find dead code while refactoring, note it but do not delete it in the same commit. Suggest a follow-up.

## 5. Verify + summarize

After the refactor:
- Re-run the full test suite.
- Report: "refactored X into Y+Z, all N tests still pass."
- If behavior changed inadvertently, surface it immediately — do not hide it.
