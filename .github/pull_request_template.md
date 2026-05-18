<!--
Thanks for the PR. Please fill the checklists below.
For tryopencomputer.com / SaaS work, the security checklist is mandatory — see
OpenComputer/docs/THREAT-MODEL.md, OpenComputer/docs/SECURITY-INVARIANTS.md,
OpenComputer/docs/ARCHITECTURE.md, and the canonical plan at
OpenComputer/docs/plans/tryopencomputer-platform-build-2026-05-18.md
-->

## Summary

<!-- One paragraph: what this PR changes and why. -->

## Phase / scope

<!-- If this PR is part of the tryopencomputer.com build, name the phase. Otherwise: "n/a — standalone OC work". -->

- Plan phase: <!-- e.g. Phase 1a · oc · env-pinnable dashboard token -->
- Repo tag: `[oc]` / `[ocp]` / `[both]` / `[ops]` / n/a

## Security checklist (mandatory for tenant-scoped or platform code; skip with "n/a — standalone OC" if unrelated)

- [ ] **Touches a tenant-scoped resource?** If yes — RLS policy reviewed (`oc-platform/packages/db/`) and named in this PR description.
- [ ] **Adds a new endpoint?** If yes — does it accept a tenant id from the client? Must be **NO** (Invariant 2). Confirm: __________
- [ ] **Tenant isolation tests touched/added** in `oc-workspace/tests/test_tenant_isolation.spec.ts` (if endpoint or routing change).
- [ ] **No client-supplied `userId` / `vmId` / `tenantId`** read in any routing/proxy decision. (`grep -RE "req\.(body|query|params|headers)\.(user|tenant|vm)Id" oc-workspace/server/` clean.)
- [ ] **Token lifetimes correct** — capability tokens ≤ 5 min, per-VM Bearer rotated atomically with KMS update if changed.
- [ ] **No long-lived secret added to VM image / cloud-init.** (Hetzner / AWS / Razorpay / Supabase service-role keys stay off the VM.)
- [ ] **No new `0.0.0.0` bind** introduced on any code path that runs on a user VM (Invariant 1).
- [ ] **THREAT-MODEL / SECURITY-INVARIANTS / ARCHITECTURE updated** if the change alters a trust boundary, an invariant, or a component (and mirrored to `oc-platform/docs/`).
- [ ] **Plan doc updated** — phase status, exit criteria, and Notes log entry if this PR advances a phase.

## General checklist

- [ ] Tests added / updated (or stated explicitly why none).
- [ ] `pytest tests/` green locally (for `[oc]` changes touching Python).
- [ ] `npm test` / `npm run lint` green locally (for `[ocp]` or `oc-workspace` JS/TS changes).
- [ ] No new dependency added without a one-line justification.
- [ ] Docstrings / comments only where they explain *why*, not *what*.

## Out-of-scope / follow-ups

<!-- Anything intentionally left for a later PR. Be explicit. -->

## Cross-repo links

<!-- If this is one half of a `[both]` change, link the matching PR in the other repo. -->
