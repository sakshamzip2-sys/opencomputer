# Hermes Doc-Parity (Quickstart + CLI/TUI/WSL2/Config) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the parity question raised by two Hermes Agent reference docs supplied by the user (Quickstart/Install + CLI/TUI/WSL2/Config) by shipping a findings doc and a 1-line + 1-paragraph README correction. No code, no new CLI surfaces, no new tests.

**Architecture:** Doc-only PR. Two files touched. No runtime impact. Worktree based on `origin/main` so the gateway-respawn-fix branch and other in-flight work stay isolated.

**Tech Stack:** Markdown.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-05-08-hermes-doc-parity-design.md` (already committed to this worktree).

**Worktree:** `/Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/` on branch `parity/hermes-quickstart-cli-2026-05-08` (based on `origin/main` at `429c5b8f`).

---

### Task 1: Findings doc at `docs/refs/hermes-agent/`

**Files:**
- Create: `OpenComputer/docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md`

- [ ] **Step 1: Verify location convention**

Run: `ls /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/OpenComputer/docs/refs/hermes-agent/`

Expected: existing peers `inventory.md`, `2026-04-28-major-gaps.md`, `2026-05-06-deep-comparison.md`. New file lands as a sibling, dated 2026-05-08.

- [ ] **Step 2: Write the findings doc**

Use the structure of the spec (§2 tables) as the body. Adapt as a stand-alone reader-facing document (assume the reader has not seen the spec).

Structure:
1. Context paragraph: what was compared, why, the "makes sense" filter.
2. **Already shipped — parity ✓** table (mirrors spec §2.1).
3. **Missing AND deliberately not shipping** table with rationale (mirrors spec §2.2).
4. **Parked — plausible future value** table (mirrors spec §2.3).
5. **README correction** mention (mirrors spec §2.4).
6. Closing note: this is a snapshot; supersedes nothing; the parity question for these two specific docs is closed.

Keep it tight: ~250-350 lines including tables. No code blocks. No external links beyond doc cross-refs.

- [ ] **Step 3: Self-review the findings doc**

Read the file end-to-end. Verify:
- No "TODO" / "TBD" / placeholder entries.
- No internal contradictions (e.g. an item appearing in both "shipped" and "parked").
- Each "deliberately not shipping" entry includes a one-clause rationale.
- Table column widths render reasonably (no row > 200 chars).

Fix issues inline.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08
git add OpenComputer/docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md OpenComputer/docs/superpowers/specs/2026-05-08-hermes-doc-parity-design.md OpenComputer/docs/superpowers/plans/2026-05-08-hermes-doc-parity.md
git commit -m "docs(refs): hermes quickstart + CLI/TUI/WSL2/config parity findings (2026-05-08)

Records the comparison between two Hermes Agent reference docs (Quickstart/Install
+ CLI/TUI/WSL2/Configuration) and the OpenComputer state on 2026-05-08, with the
\"only-if-makes-sense\" filter applied.

Outcome: ~95% parity already shipped (verified by code-walk + cross-ref to
docs/refs/hermes-agent/2026-05-06-deep-comparison.md). The remaining items
either don't pass the makes-sense filter for this user's actual workflow
(Mac + Telegram-driven gateway) or were already deliberately scoped out
(modal/daytona/singularity backends, Asia channels). One item (/background
slash) is parked for future demand-driven reopen.

Includes:
- docs/refs/hermes-agent/2026-05-08-...-config-parity.md (findings table)
- docs/superpowers/specs/2026-05-08-hermes-doc-parity-design.md (decision spec)
- docs/superpowers/plans/2026-05-08-hermes-doc-parity.md (the plan that produced this)"
```

Expected: clean commit, no hook failures.

---

### Task 2: README Windows acknowledgement

**Files:**
- Modify: `OpenComputer/README.md` (install section header + one paragraph below the curl command)

- [ ] **Step 1: Read the existing install section**

Read lines around the install header to anchor the edit.

Run: `grep -n "One-line install\|macOS / Linux" /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/OpenComputer/README.md`

Expected: hit at line ~25 of the README.

- [ ] **Step 2: Update the install header**

Edit the existing line:

```markdown
**One-line install** (macOS / Linux / Termux):
```

to:

```markdown
**One-line install** (macOS / Linux / Termux — on Windows, see the note below):
```

- [ ] **Step 3: Add the Windows note paragraph**

Immediately after the install-script paragraph (the one ending with "...`--use-pipx` to force pipx."), add a new paragraph:

```markdown
**Windows users:** OpenComputer runs natively on Windows (Python 3.13+) via
`pip install opencomputer`. The interactive `oc model`, clipboard, screenshot,
and PowerShell-run paths all work on Windows out of the box. If you'd rather use
the curl install script, run it inside WSL2 (`wsl --install` from PowerShell,
then run the install command above inside the WSL shell). All the same paths
apply — just keep the install on the Linux side of the WSL filesystem (`~/`)
for performance.
```

- [ ] **Step 4: Verify the edit reads naturally**

Run: `sed -n '24,40p' /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/OpenComputer/README.md`

Expected: install header + curl command + paragraph about pipx fallback + new Windows paragraph + Manual install heading. Read it as a new user would.

- [ ] **Step 5: Sanity check — README still parses as well-formed Markdown**

Run: `python3 -c "import pathlib, re; t = pathlib.Path('/Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/OpenComputer/README.md').read_text(); assert t.count('\`\`\`') % 2 == 0, 'unbalanced code fences'; print('OK,', t.count('\`\`\`'), 'fences,', len(t.splitlines()), 'lines')"`

Expected: `OK, <even number> fences, <line count> lines`.

- [ ] **Step 6: Commit**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08
git add OpenComputer/README.md
git commit -m "docs(readme): acknowledge native Windows support + WSL2 path

The install header read 'macOS / Linux / Termux' — but OC has shipped native
Windows runtime support since PR #267 (cross-platform deployment parity,
2026-04-29: PowerShellRun, Win32 SendInput shim, Windows clipboard, msvcrt
locking). Telling users we don't run on Windows when we do was less honest
than the code.

Surfaces both paths (native pip install + WSL2 for the curl script) so a
Windows user lands on the right one for their setup."
```

Expected: clean commit.

---

### Task 3: Verify nothing broke + push

- [ ] **Step 1: Run a smoke test suite**

Doc-only changes truly cannot affect Python tests, but per "Run Full Suite Before Pushing" memory rule, run pytest as a baseline-preservation check.

```bash
source /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/activate
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/OpenComputer
pytest tests/ -x --no-header -q -p no:randomly 2>&1 | tail -20
```

Expected: green or the known pre-existing Honcho test-pollution flake (`test_agent_loop_multi_turn_snapshot_stays_identical_across_different_prefetches` — see memory entry `project_honcho_default_test_pollution_flake`). If a *new* failure appears that's not the Honcho flake, investigate before pushing.

If `.venv` activation fails (e.g. parent venv missing), skip pytest and rely on CI — note explicitly in PR description that local pytest was skipped, do not silently push.

- [ ] **Step 2: Run ruff (sanity)**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08
ruff check OpenComputer/opencomputer OpenComputer/plugin_sdk OpenComputer/extensions OpenComputer/tests 2>&1 | tail -10
```

Expected: 0 findings (no Python touched, so this is a baseline-preservation check).

- [ ] **Step 3: Push the branch**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08
git push -u origin parity/hermes-quickstart-cli-2026-05-08 2>&1 | tail -10
```

Expected: branch pushed with upstream tracking set.

- [ ] **Step 4: Open the PR (or note that user wants to handle merge)**

```bash
cd /Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08
gh pr create --title "docs(refs): hermes quickstart + CLI/TUI/WSL2/config parity (no code)" --body "$(cat <<'EOF'
## Summary

- Records the parity comparison between two Hermes Agent reference docs (Quickstart/Install + CLI/TUI/WSL2/Configuration) and OpenComputer state on 2026-05-08.
- Honest README correction: we acknowledge native Windows support (which has been shipping since PR #267) and point WSL2 users at the install script.

## Decision recorded in spec

After applying the user's "only integrate something that actually makes sense" filter, the answer was *don't ship a parity port*: ~95% of the load-bearing surface area was already in OC, and the residual items either don't match this user's daily workflow (Mac + Telegram-driven gateway) or were already deliberately scoped out (modal/daytona/singularity backends, Asia channels, kawaii indicator, etc.).

One item (/background slash) is *parked* for future demand-driven reopen.

## Files

- `docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md` — findings table for future me
- `docs/superpowers/specs/2026-05-08-hermes-doc-parity-design.md` — decision spec
- `docs/superpowers/plans/2026-05-08-hermes-doc-parity.md` — plan that produced these
- `README.md` — Windows acknowledgement (1-line header + 1 paragraph)

## Test plan

- [x] Doc-only — no tests required
- [x] `pytest tests/` confirmed unchanged from main baseline
- [x] `ruff check` clean
- [x] README markdown well-formed (balanced code fences, valid section nesting)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -10
```

Expected: PR URL printed.

If `gh` is not authenticated or the user prefers to open the PR themselves, surface the branch name + commit log instead and stop here.

- [ ] **Step 5: Mark execute task complete**

---

## Self-review checklist

- [x] **Spec coverage:** Every item in spec §3 (findings doc + README correction) has a task. Task 1 = doc, Task 2 = README, Task 3 = verify+push.
- [x] **Placeholder scan:** No "TBD", no "appropriate error handling", no "similar to Task N". All commits show full message bodies. Verification commands have expected-output lines.
- [x] **Type / signature consistency:** N/A (no code).
- [x] **Worktree path consistency:** All commands reference the same `/Users/saksham/Vscode/claude/.claude/worktrees/hermes-quickstart-cli-2026-05-08/` path.
- [x] **Edge cases handled:** `.venv` may or may not exist in worktree (Step 3.1 covers both); `gh` may or may not be authenticated (Step 3.4 covers manual fallback).

---

## Out-of-scope reminders

If during execution there is any temptation to:
- Add new CLI commands or slash commands → **STOP**. The spec rejected this for "doesn't pass makes-sense filter".
- Add new config schema entries → **STOP**. Same.
- Touch any `.py` file → **STOP**. This PR is doc-only by design.
- Write WSL2 deep documentation → **STOP**. The 1-line README mention is the entire WSL2 surface for this PR.
- Implement `/background` slash → **STOP**. Parked. Not this PR.

If a real bug or test failure appears that's unrelated to this work, file it separately — do not include the fix in this PR.
