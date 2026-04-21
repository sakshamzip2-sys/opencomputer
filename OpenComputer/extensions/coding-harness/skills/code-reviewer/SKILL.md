---
name: Code reviewer
description: Use when the user asks to review a pull request, review a diff, audit recent changes, code review, check this PR, check my changes, or wants critique on a code change before merging.
version: 0.1.0
---

# Code Reviewer

Reviewing a diff or PR. Follow this order.

## 1. Understand the change

- Read the PR title and description — what is this supposed to do?
- Skim the full diff to get scope. Note: new files, renames, deletes, public API changes.
- If no diff is provided, ask the user for the branch/commit range, or run `git diff <base>...HEAD`.

## 2. Check the four critical things

**Correctness**
- Obvious bugs, wrong logic, off-by-one, null handling
- Does the code do what the PR description claims?
- Edge cases: empty inputs, large inputs, concurrent calls, failure paths

**Security**
- Untrusted input → validation, escaping
- Secrets in code or logs (API keys, tokens)
- Injection vectors (SQL, shell, path traversal)
- Auth/authz changes — who can now do what?

**Tests**
- Are there tests for the new code? Do they actually exercise the new behavior?
- Do tests pass? If CI is red, flag it.
- Is there a test for the failure case, not just happy path?

**Maintainability**
- Clear naming, focused functions
- Dead code? Commented-out code?
- Broken project conventions?

## 3. Output format

Return findings grouped by severity:

- **BLOCKING** — must fix before merge. Each one includes: file:line, what's wrong, suggested fix.
- **SHOULD FIX** — not blocking but valuable. Same format.
- **NITS** — style, naming, tiny readability wins. Keep brief.
- **PRAISE** — if part of the diff is genuinely good, say so. (Keeps review honest.)

## 4. Don't

- Don't rewrite the code — describe the fix, let the author implement.
- Don't demand style changes the codebase doesn't already enforce.
- Don't flag "no test" without suggesting what test would catch the bug.
- Don't be exhaustive on small diffs. 5 findings on a 10-line PR is noise.

## 5. When you're done

If every check passes: **"LGTM. Nothing blocking."**
If there's a blocker: escalate clearly.
