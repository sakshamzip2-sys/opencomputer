---
name: dependency-audit
description: Use when reviewing project dependencies, addressing vulnerability reports, npm audit, pip-audit, or upgrade planning
---

# Dependency Audit

## When to use

- Fresh repo / pre-deploy security check
- Dependabot / Renovate PR landed
- Vulnerability disclosure for a package you use

## Steps

1. **Scan first.**
   - Python: `pip-audit` or `safety check`.
   - Node: `npm audit --omit=dev` (prod-only) and full audit.
   - Cross-language: `osv-scanner` for OSV database.
2. **Triage by exploitability, not severity.** "Critical CVE in a transitive dev dependency" is rarely an emergency. Direct + prod + reachable code path = real.
3. **Pin transitively.** Lock files are the contract; `requirements.txt` without versions is a foot-gun.
4. **Upgrade in batches by package family.** All `@aws-sdk/*` together; not one at a time.
5. **Test before merge.** Dependency upgrades must run the full suite + integration tests.
6. **Watch for typosquatting.** Especially in npm. New deps from PRs deserve a careful look at the registry page.
7. **Track abandoned deps.** Last release > 18 months ago + open security issues = plan a replacement.

## Notes

- `pip install --upgrade X` doesn't update transitive deps — use `pip install -U` with the full set or use `uv lock`.
- `npm ci` is for CI; `npm install` updates `package-lock.json` and can drift.
- Pin major + minor; allow patches: `^1.2.3` (npm) / `~=1.2` (Python) is usually right.
