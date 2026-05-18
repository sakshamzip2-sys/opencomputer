# tryopencomputer.com — Security invariants

> **Status:** Phase 0 deliverable. Non-negotiable rules.
> **Mirrors:** `oc-platform/docs/SECURITY-INVARIANTS.md` (identical content; update both together).
> **Canonical plan:** [`docs/plans/tryopencomputer-platform-build-2026-05-18.md`](plans/tryopencomputer-platform-build-2026-05-18.md)
> **Threat model:** [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
> **Last updated:** 2026-05-18

Every invariant below has the same structure:

1. **The rule** — stated as something that is or is not true of the system.
2. **What violates it** — concrete examples of code/config/operator action that breaks the invariant.
3. **How it is enforced** — the automated check (CI, lint, test) or manual gate that proves the invariant holds.
4. **What to do if it ever fails** — the response runbook so we are not improvising under pressure.

If an invariant cannot be automatically enforced, it has a manual gate; if it has neither, it is a goal, not an invariant, and does not belong here.

---

## Invariant 1 — No public listener on any user VM

### The rule
Every Hetzner VM provisioned for a tryopencomputer.com user accepts ZERO inbound TCP/UDP connections from the public internet. All traffic to the VM arrives through the Cloudflare tunnel that the VM dials OUT to.

### What violates it
- Any port exposed on the VM's public Hetzner IP (cloud-init opens `:9119`, `:6080`, `:22`, etc. on `0.0.0.0`).
- A misconfigured Hetzner Cloud firewall (default-deny-inbound rule removed).
- A debugging shell that runs `python -m http.server 0.0.0.0:8000` and is left running.
- An `oc` command (e.g. `oc workspace backend`) defaulting to `0.0.0.0` bind instead of `127.0.0.1`.

### How it is enforced
- **CI check** in `oc-platform`: `nmap -Pn -p- <vm-ip>` against a freshly provisioned test VM in the image-build pipeline. **All ports MUST be `filtered` or `closed`.** Any `open` port fails the build.
- **Hetzner Cloud firewall** attached to every VM by default, dropping all inbound. The firewall rule is enforced by the `service-api` provisioning step and asserted post-provision.
- **OpenComputer guard:** `oc workspace backend` refuses to bind to `0.0.0.0` unless `OC_DASHBOARD_ALLOW_EXTERNAL_BIND=1` is set (consent prompt + audit log).
- **Visual confirmation:** `service-api` exposes a `GET /vms/:id/exposure-report` admin endpoint that runs the nmap from a known external prober and returns the result.

### What to do if it ever fails
1. **Immediate:** revoke the affected VM's Cloudflare tunnel; the VM becomes unreachable from the platform (degraded but contained).
2. **Triage:** dump VM `iptables-save` + `nft list ruleset` to determine how the listener appeared.
3. **Rotate:** all per-VM tokens for VMs provisioned in the same image generation; rebuild the image.
4. **Postmortem:** add a new CI gate that would have caught the specific failure mode.

---

## Invariant 2 — No client-controlled tenant identifier

### The rule
The target VM, user, or tenant for any request reaching `oc-workspace`'s BFF is a pure function of the **server-verified Supabase JWT** in the `HttpOnly` session cookie. NO client-supplied field (URL param, body, header, query string) is ever consulted to determine "which tenant does this request belong to."

### What violates it
- `req.body.userId` / `req.query.vmId` / `req.params.tenantId` / `req.headers['x-tenant-id']` read in any routing or proxy decision.
- A capability token issued without a server-side `sub` lookup (e.g. minting one from a client-supplied id).
- A SQL query in the BFF that uses a client-supplied id instead of `auth.uid()` / the verified session subject.
- A proxy endpoint that accepts a hostname from the client (e.g. `?target=agent-xxx.oc-vms.example.com`) and forwards to it.

### How it is enforced
- **Lint rule** (`oc-workspace/eslint-rules/no-client-tenant-id.js`): AST-level grep for `req.{body,query,params,headers}.*` accessed in any file under `server/api/`, `server/proxy/`, `server/routes/`. Allowed only when followed by a `verifySession()` + server-side id resolution.
- **CI gate:** `grep -RE 'req\.(body|query|params|headers)\.(user|tenant|vm)Id' oc-workspace/server/ | grep -v "// LINT-ALLOW:"` must return zero.
- **Adversarial test** in `oc-workspace/tests/test_tenant_isolation.spec.ts`: forges a body/header with another user's id and confirms the routing decision ignores it.
- **Code review:** PR template's checkbox: _"Adds a new endpoint? Does it accept a tenant id from the client?"_ — must be answered **NO** or the PR is blocked.

### What to do if it ever fails
1. **Immediate:** revert the offending commit; redeploy.
2. **Audit:** check audit logs for any cross-tenant routing decisions during the window the bad code was live.
3. **Notify:** affected users if cross-tenant access actually occurred (legal + comms).
4. **Strengthen:** convert the lint from line-grep to AST and add a unit test that exercises the exact endpoint shape that slipped through.

---

## Invariant 3 — Postgres RLS on every tenant-scoped table BEFORE first real user

### The rule
Every Postgres table that contains user-scoped data has Row-Level Security ENABLED with a policy that restricts reads/writes to `auth.uid() = <owner_column>`. No exceptions, no "we'll add it later for this table."

### What violates it
- Creating a new tenant-scoped table without an `OWNER` policy.
- Dropping or disabling RLS on an existing table (`ALTER TABLE ... DISABLE ROW LEVEL SECURITY`).
- Using the Supabase service-role key from `oc-workspace` for a query that could have run as the user (service role bypasses RLS).
- Inserting a row whose `owner_id` is not `auth.uid()`.

### How it is enforced
- **CI gate** (`oc-platform/scripts/check_rls.ts`): connects to a clean test Postgres, queries `pg_tables` for tables not in `(_realtime, auth, storage, ...)`, and confirms `rowsecurity = true` for each. CI fails if a table exists without RLS.
- **Drizzle migration review:** every migration that creates a table must include a corresponding `CREATE POLICY` statement; the schema reviewer rejects PRs missing one.
- **Service-role audit:** the BFF's service-role calls are isolated to `server/admin/*` files; a separate lint rule rejects service-role imports anywhere else.

### What to do if it ever fails
1. **Immediate:** re-enable RLS on the affected table; add the missing policy.
2. **Audit:** scan query logs for the window of unprotected access; identify which user-id pairs were queried by which sessions.
3. **Notify:** users affected if cross-tenant reads actually occurred.
4. **Backport:** add the table to the RLS test list explicitly (named-table assertion, not just count-based).

---

## Invariant 4 — Per-VM `OC_DASHBOARD_TOKEN` env-pinned and verified VM-side

### The rule
Every VM has a unique `OC_DASHBOARD_TOKEN` injected as an environment variable at provision time. `oc workspace backend` MUST read this env var on startup and use it as the only valid Bearer for incoming requests. The token is NEVER regenerated at process boot from `secrets.token_urlsafe(32)` — that would silently invalidate the platform's view of the token on every restart.

### What violates it
- Reverting [`OpenComputer/opencomputer/dashboard/server.py`](../opencomputer/dashboard/server.py) (search for `OC_DASHBOARD_TOKEN` — the env-override block lives ~line 60–85 as of Phase 1a, 2026-05-18) to the pre-Phase-1a shape `_SESSION_TOKEN: str = secrets.token_urlsafe(32)` — would silently regenerate a fresh token on every process start, defeating env-pinning.
- A VM startup script that fails to export `OC_DASHBOARD_TOKEN` before launching `oc`.
- A code path that accepts a Bearer matching `secrets.token_urlsafe(32)` shape regardless of env var (legacy debug shortcut).
- Logging the token to stdout / syslog / the audit log in plaintext.
- Empty-string env (`OC_DASHBOARD_TOKEN=`) being accepted as a valid pinned token. Handled by `or` shortcut today — must stay handled.

### How it is enforced
- **OpenComputer tests** ([`OpenComputer/tests/test_dashboard_token_env_override.py`](../tests/test_dashboard_token_env_override.py), 7 tests) assert:
  (a) `OC_DASHBOARD_TOKEN=<value>` pins `_SESSION_TOKEN` to that exact value,
  (b) the pinned value propagates to `app.state.session_token`,
  (c) two reimports with the same env yield the same token (stable across simulated restart — the actual production property),
  (d) two reimports without the env yield DIFFERENT tokens (random fallback works),
  (e) empty-string env (`OC_DASHBOARD_TOKEN=`) falls through to the random fallback — empty Bearer is never accepted.
- **Cloud-init template (`oc-platform/templates/cloud-init.yaml.tmpl`):** asserts (via `cloud-init validate` + provision-time test) that `OC_DASHBOARD_TOKEN` is set in `/etc/systemd/system/oc-workspace.service.d/override.conf`.
- **Service-api provision step** records the token in the `vms.vm_token` column (KMS-encrypted) at the same moment it's baked into the VM env. Mismatch impossible at provision time; rotation is an atomic update of both.
- **Log scrub:** structured logger has a redaction list that masks anything matching the token shape.

### What to do if it ever fails
1. **Immediate:** for any affected VM, force-rotate via `service-api`: stop VM, regenerate token, update KMS row, restart VM with new env.
2. **Audit:** check the BFF's logs for any successful proxy decisions with the old (post-restart auto-generated) token — those are the false positives.
3. **Bake in:** add an end-to-end test that restarts the VM and confirms the token in `vms.vm_token` still authorizes; a regression would fail this immediately.

---

## Invariant 5 — Adversarial cross-tenant tests green in CI before each phase merges

### The rule
The `oc-workspace/tests/test_tenant_isolation.spec.ts` adversarial suite (and any phase-specific equivalents) MUST pass in CI for every PR. A failure of any adversarial test blocks merge.

### What violates it
- A PR that skips, `xit`/`xtest`-marks, or removes an adversarial test without replacing it with a stronger one.
- A PR that disables CI for the test file ("flaky, temporarily skipping").
- A PR that merges with a pending failure ("we'll fix it next PR").

### How it is enforced
- **GitHub branch protection** on `main` (both repos): `tenant-isolation` workflow must be green to merge.
- **Per-phase exit criterion** (in plan §8): each phase's exit criteria includes "adversarial suite green."
- **No-skip linter:** custom CI step rejects new `it.skip` / `xit` / `xtest` in `tests/test_tenant_isolation.spec.ts` unless paired with a `TODO(SEC-<n>):` referencing an open issue.

### What to do if it ever fails
1. **Immediate:** stop the merge. Do NOT bypass branch protection.
2. **Diagnose:** is this a real cross-tenant leak, a flaky test, or an over-strict assertion? Default assumption: it's real.
3. **Fix:** the code, not the test. If the test must change, the change is reviewed by at least one person who didn't write it.
4. **Document:** every fix lands with a paragraph in the THREAT-MODEL.md row it relates to.

---

## Invariant 6 — No phase ships to production without the previous phase's exit criteria green

### The rule
The build phases in `docs/plans/tryopencomputer-platform-build-2026-05-18.md` §8 are SEQUENTIAL. Phase N+1 work may start in a branch, but Phase N+1 cannot ship to production until Phase N's exit criteria are all 🟢 in the plan doc.

### What violates it
- Deploying Phase 2 reverse-tunnel work while Phase 1a (env-pinned token) is still ☐.
- Marking a phase 🟢 in the plan while one of its exit criteria is still ☐.
- Skipping a phase ("we'll come back to capability tokens").

### How it is enforced
- **The plan doc itself** — its update protocol (§0) requires every status change be reviewed at the start of each session and tied to a Notes log entry.
- **Manual operator gate:** the cutover step (Phase 8) explicitly re-verifies all prior phases are 🟢.
- **Deploy gate:** the `oc-platform` CD workflow refuses to deploy to production if `docs/plans/tryopencomputer-platform-build-2026-05-18.md` contains any unchecked exit-criteria checkbox for the current phase or earlier. (Phase 6 deliverable.)

### What to do if it ever fails
1. **Immediate:** roll back the unauthorized deploy.
2. **Audit:** what changed in production that should not have?
3. **Recover:** complete the skipped phase before redeploying.
4. **Tighten:** add the specific check to the deploy gate.

---

## Invariant 7 — No real customer signups until third-party pentest passes

### The rule
Phase 7 (external validation) MUST be completed with all High/Critical findings resolved before the public DNS cutover that allows real users to sign up. Until then, signups are gated by a manual allow-list (friends-and-family / internal-only).

### What violates it
- Pointing `tryopencomputer.com` MX/A records at the production stack before Phase 7 is 🟢.
- Removing the allow-list check from `oc-workspace`'s signup endpoint.
- Accepting payments from non-allow-listed users.

### How it is enforced
- **DNS-level gate:** the production hostname doesn't exist publicly until Phase 8.
- **Code-level gate:** `oc-workspace/server/api/auth/sign-up.ts` includes an `ALLOWED_EMAILS` allow-list (env var); removing this check requires a PR that touches this very document.
- **Razorpay gate:** the production Razorpay merchant account is in test mode until Phase 8 cutover.

### What to do if it ever fails
1. **Immediate:** put the signup endpoint back behind the allow-list; revoke any signups created in the gap.
2. **Notify:** all users created during the gap (this is a public commitment).
3. **Refund:** any payments collected.
4. **Reset:** treat the cutover gate as a tripwire and rehearse it.

---

## How this document gets updated

- Adding a new invariant requires it pass the four-section test: rule, violation, enforcement, response.
- Removing an invariant requires explicit decision-log entry in the plan doc explaining what replaced it.
- Mirror every change to `oc-platform/docs/SECURITY-INVARIANTS.md` in the same PR pair.
- Cross-reference Actor rows in `docs/THREAT-MODEL.md` where relevant.

## Cross-references

- Threats: [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
- Architecture: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
- Plan: [`docs/plans/tryopencomputer-platform-build-2026-05-18.md`](plans/tryopencomputer-platform-build-2026-05-18.md) §7 (summary) and §8 (phase-by-phase enforcement).
