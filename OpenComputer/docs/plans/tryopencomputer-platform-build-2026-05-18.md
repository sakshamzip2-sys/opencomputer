# `tryopencomputer.com` — Platform Build Plan

> **A living, trackable plan for unifying `oc-platform`'s SaaS layer + `oc-workspace`'s
> per-agent UI into one frontend deployed at `tryopencomputer.com`, talking to
> per-user Hetzner VMs running OpenComputer over reverse tunnels.**

| | |
|---|---|
| **Plan ID** | `tryopencomputer-2026-05-18` |
| **Status** | 🟡 Phase 0 — Foundations (not started) |
| **Started** | 2026-05-18 |
| **Last updated** | 2026-05-18 |
| **Doc owner** | Archit (`sakriarchit@gmail.com`) |
| **Canonical location** | `opencomputer/OpenComputer/docs/plans/tryopencomputer-platform-build-2026-05-18.md` |
| **Mirror in `oc-platform`** | `oc-platform/docs/tryopencomputer-plan.md` (pointer to canonical) — to be created at Phase 0 |

---

## 0. How to use this document

This is a **living plan**. Update it every work session.

### Status legend (per phase / per task)
- 🟢 **Done** — merged + verified
- 🟡 **In progress** — being worked on
- 🔴 **Blocked** — waiting on something explicit
- ☐ **Not started** — ready, nothing blocking, nobody's picked it up

### Update protocol (do this every session)

1. At the **top of the doc**, bump `Last updated` and the overall `Status`.
2. In whatever **Phase** you worked on:
   - Flip task statuses (☐ → 🟡 → 🟢).
   - Append a one-line entry to that phase's **Notes / Decisions log** describing what happened.
3. Anything new you decided (architecture, library choice, naming) → append to the top-level **§9 Decision Log**.
4. Anything you got stuck on → append to **§10 Open Questions**.
5. Commit the updated doc on the same branch as the work (so they're reviewed together).

### Cross-repo task tags
Every task has a tag indicating where it lives:
- `[oc]` — `opencomputer` repo (this one)
- `[ocp]` — `oc-platform` repo
- `[both]` — both repos, coordinated
- `[ops]` — infrastructure / operations (DNS, Cloudflare, Hetzner console, Supabase dashboard) — no repo

---

## 1. Table of contents

- [0. How to use this document](#0-how-to-use-this-document)
- [1. Table of contents](#1-table-of-contents)
- [2. Context — where we're starting from](#2-context--where-were-starting-from)
- [3. Goal & vision](#3-goal--vision)
- [4. Repo map & coordination](#4-repo-map--coordination)
- [5. Architecture (target state)](#5-architecture-target-state)
- [6. Threat model](#6-threat-model)
- [7. Security invariants (non-negotiables)](#7-security-invariants-non-negotiables)
- [8. Build phases](#8-build-phases)
  - [Phase 0 — Foundations](#phase-0--foundations)
  - [Phase 1 — Foundational primitives](#phase-1--foundational-primitives)
  - [Phase 2 — Reverse tunnel (VM no public ports)](#phase-2--reverse-tunnel-vm-no-public-ports)
  - [Phase 3 — Capability tokens](#phase-3--capability-tokens)
  - [Phase 4 — `oc-workspace` becomes the BFF + proxy](#phase-4--oc-workspace-becomes-the-bff--proxy)
  - [Phase 5 — VM-side defense layers](#phase-5--vm-side-defense-layers)
  - [Phase 6 — Operational hardening](#phase-6--operational-hardening)
  - [Phase 7 — External validation](#phase-7--external-validation)
  - [Phase 8 — Cutover](#phase-8--cutover)
- [9. Decision log](#9-decision-log)
- [10. Open questions](#10-open-questions)
- [11. Risk register](#11-risk-register)
- [12. Glossary](#12-glossary)
- [13. Pre-launch final checklist](#13-pre-launch-final-checklist)

---

## 2. Context — where we're starting from

This plan grew out of a long planning conversation on **2026-05-18**. Background a fresh reader needs:

### What `opencomputer` (`[oc]`) is today

A personal AI-agent framework in Python 3.12+. Public repo `sakshamzip2-sys/opencomputer`. Architecture diagram in [`OpenComputer/CLAUDE.md`](../../CLAUDE.md).

**Active surfaces (post-2026-05-18 cleanup):**
- `oc chat` — in-process terminal chat (the workhorse)
- `oc workspace` — Hermes Workspace browser UI on `:3002`, backend on `:9119`
- `oc workspace backend` — the FastAPI backend standalone (Phase 2 of session-cleanup, PR #651)
- `oc gateway` — channel daemons (Telegram/Slack/Discord/…)
- `oc wire` — WebSocket JSON-RPC :18789 (consumed by `oc tui`)
- `oc tui` — Ink/React full-screen terminal UI; source revived in PR #641, build it yourself
- `oc dashboard` — DEPRECATED forwarding shim to `oc workspace backend` (PR #651)

**Recent cleanup landed on `main` 2026-05-18:**
- PR #651 — removed dead `oc webui`, folded `oc dashboard` into `oc workspace backend`, renamed `'webui'` session-source label to `'workspace'` + v22 DB migration.
- PR #653 — 17 `computer-use` commits (cua-driver 0.1.9 reconciliation + audit hardening).

### What `oc-platform` (`[ocp]`) is today

Separate repo at `~/Documents/GitHub/oc-platform/`. SaaS that provisions per-user Hetzner VMs running OC. Architecture diagram in `oc-platform/CLAUDE.md` and `README.md`.

**Three packages + a VM template:**
| Package | Tech | Job |
|---|---|---|
| `packages/frontend` | **Next.js 14** | Landing, sign-in (Supabase), `dashboard/` (deploy/status), `dashboard/desktop` (noVNC), `dashboard/billing` (Razorpay), `dashboard/settings` |
| `packages/service-api` | **Hono / Node** on AWS, port 3001 | REST endpoints: `/api/instance/*` (deploy/stop/restart/delete/desktop/progress), `/api/billing/*`, `/api/user/me`. Auth = Supabase JWT (verified via JWKS). |
| `packages/oc-compute` | **Cloudflare Worker + Durable Object** | Fleet manager — `POST /v1/leases` → Hetzner API → creates VM |
| `templates/cloud-init.yaml.tmpl` | YAML | What boots on each VM: Xfce + TigerVNC + noVNC + OpenComputer + systemd units |

**Today's VM cloud-init runs:**
- `oc gateway run` (the agent, as `root`)
- `oc dashboard --host 0.0.0.0 --port 9119 --insecure` ← **wide open to internet** (security hole, fixed by this plan)
- noVNC `--listen 6080` (also wide open; VNC password literally `"password"`)

### Security holes in the current state (fixed by this plan)

1. VM's `oc dashboard` bound to `0.0.0.0 :9119 --insecure` — public agent API.
2. noVNC on `:6080` public; VNC password `"password"` hardcoded.
3. Agent runs as root with full internet access. **Decision (2026-05-18): root is the product, not a bug — agent owns its computer. The boundary is the VM's tunnel, not what runs inside.** See §9 Decision Log.
4. Anthropic API key baked into cloud-init userData (readable via metadata service).
5. Progress callback gated by a UUID (an identifier, not a secret).
6. VM IP recycling risk if a deleted VM's user→VM row isn't torn down atomically.

---

## 3. Goal & vision

**Collapse `oc-platform`'s SaaS layer into `oc-workspace` so that one frontend serves everything.**

- `tryopencomputer.com` = the new `oc-workspace` (TanStack Start / Vite, Node SSR — already exists in `opencomputer/OpenComputer/oc-workspace/`).
- User flow: visit tryopencomputer.com → sign up (Supabase) → subscribe (Razorpay) → create their agent → **a Hetzner VM is provisioned with OpenComputer on it ("agent has its own computer")** → user controls the agent through `oc-workspace`'s chat/sessions/skills tabs (proxied to that user's VM) + a noVNC desktop tab (also proxied).
- **One login. One origin. One frontend.** The browser never sees a VM IP, never sees a VM token.

`oc-workspace` becomes a **two-layer app**:
- **Outer SaaS shell:** `/` (landing), `/sign-in`, `/billing`, `/deploy`, `/settings`.
- **Inner per-agent workspace:** `/app/*` — chat, sessions, skills, MCP, desktop — all proxied to *this logged-in user's VM*.

### What this plan does NOT change

- The agent runs as `root` on the VM. ✅
- LLM key handling: via the user's own proxy service + their JWT (not in cloud-init). ✅
- Rate limiting at oc-workspace's BFF. ✅
- `oc-platform`'s `service-api` stays deployed on AWS as the control plane (auth, billing, instance lifecycle). ✅
- `oc-compute` stays as the Cloudflare Worker fleet manager. ✅
- `oc-platform/packages/frontend` (Next.js) **gets ported** into `oc-workspace`'s TanStack/Vite app — same pages, different framework. ✅

---

## 4. Repo map & coordination

```
┌──────────────────────────────────────────────────────────────────┐
│  Repo: opencomputer  (this repo, sakshamzip2-sys/opencomputer)   │
│                                                                  │
│  • OpenComputer/                  ← Python framework             │
│    - opencomputer/dashboard/      ← FastAPI backend (the         │
│                                     "backend" on every VM)       │
│    - opencomputer/gateway/        ← agent + channel daemon       │
│  • OpenComputer/oc-workspace/     ← FRONTEND (TanStack/Vite,     │
│                                     Node SSR). Becomes           │
│                                     tryopencomputer.com.         │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Repo: oc-platform                                               │
│                                                                  │
│  • packages/frontend/             ← Next.js. PORTED OUT into     │
│                                     oc-workspace, then deleted   │
│                                     here (or kept as legacy).    │
│  • packages/service-api/          ← STAYS. AWS-hosted Hono.      │
│                                     Hosts control plane:         │
│                                     auth verify, billing,        │
│                                     instance lifecycle, mints    │
│                                     per-VM tunnel + OC tokens.   │
│  • packages/oc-compute/           ← STAYS. CF Worker → Hetzner.  │
│  • packages/shared/               ← STAYS. Shared types.         │
│  • templates/cloud-init.yaml.tmpl ← REWRITTEN. Adds cloudflared, │
│                                     removes --insecure / 0.0.0.0.│
└──────────────────────────────────────────────────────────────────┘
```

### Cross-repo task tag glossary (recap)
| Tag | Meaning |
|---|---|
| `[oc]` | Change in `opencomputer` repo |
| `[ocp]` | Change in `oc-platform` repo |
| `[both]` | Both repos, coordinated change (note both PRs) |
| `[ops]` | Infrastructure work — no repo (Cloudflare API, Supabase dashboard, Hetzner Cloud console, DNS, AWS) |

### What lives where (final state)

| Concern | Repo / location |
|---|---|
| Agent runtime | `[oc]` OpenComputer (Python) |
| OC dashboard backend (per-VM) | `[oc]` `opencomputer/dashboard/` |
| Frontend (tryopencomputer.com) | `[oc]` `OpenComputer/oc-workspace/` |
| Frontend's BFF (Node SSR server, the per-user proxy) | `[oc]` `OpenComputer/oc-workspace/src/server/` |
| Control plane: auth/billing/lifecycle | `[ocp]` `packages/service-api/` |
| Per-VM tunnel + token minting | `[ocp]` `packages/service-api/services/` |
| Hetzner provisioning | `[ocp]` `packages/oc-compute/` |
| Cloud-init template | `[ocp]` `templates/cloud-init.yaml.tmpl` |
| DB schema + RLS policies | `[ocp]` `packages/service-api/drizzle/` |
| Threat model + security invariants docs | `[both]` mirrored in `docs/` of each |
| **This plan** | `[oc]` `OpenComputer/docs/plans/` (canonical); pointer mirror in `[ocp]` |

---

## 5. Architecture (target state)

```
                          Browser
                             │
              Supabase JWT (HttpOnly cookie) over TLS
                             │
                             ▼
              ┌──────────────────────────────────┐
              │  oc-workspace                    │  tryopencomputer.com
              │  (TanStack/Vite + Node BFF)      │  hosted on Vercel/Fly/Render
              │                                  │
              │  Pages:  /  /sign-in  /billing   │
              │          /deploy  /app  /desktop │
              │                                  │
              │  BFF endpoints:                  │
              │   • verifies Supabase JWT        │
              │   • session → user → tunnel      │
              │   • streams chat (SSE)           │
              │   • proxies noVNC (WebSocket)    │
              │   • attaches per-VM Bearer       │
              └──────┬───────────────┬───────────┘
                     │               │
        (Supabase JWT)               │ (signed capability tokens)
                     ▼               ▼
        ┌────────────────┐  ┌──────────────────────────────────┐
        │  Supabase      │  │  Cloudflare (tunnel ingress)      │
        │  - Auth (JWKS) │  │  agent-<uuid>.oc-vms.example.com │
        │  - DB + RLS    │  └────────────┬─────────────────────┘
        └────────┬───────┘               │ (Cloudflare ↔ cloudflared)
                 │ RLS                   │
                 ▼                       ▼
        ┌────────────────┐  ┌──────────────────────────────────┐
        │  service-api   │  │  Hetzner VM (per user)            │
        │  (AWS)         │  │                                   │
        │  - billing     │  │  cloudflared (dials OUT only)     │
        │  - lifecycle   │  │  oc gateway       (root)          │
        │  - mints       │  │  oc workspace backend             │
        │    tunnel +    │  │      127.0.0.1:9119               │
        │    OC tokens   │  │  noVNC           127.0.0.1:6080   │
        └────────┬───────┘  │  Firewall: ALL inbound DROPPED   │
                 │          └──────────────────────────────────┘
                 ▼
        ┌────────────────┐
        │  oc-compute    │  → Hetzner Cloud API
        │  (CF Worker)   │
        └────────────────┘
```

**Five independent layers an attacker must defeat simultaneously for a cross-tenant breach:**

1. Postgres **Row-Level Security** at the storage layer.
2. **Session-derived routing** — no client-influenced tenant identifier.
3. **Signed capability tokens** for resource references.
4. **Per-VM `OC_DASHBOARD_TOKEN`** verified VM-side.
5. **VM egress allow-list** + reverse tunnel (no public listener).

---

## 6. Threat model

To live as `docs/THREAT-MODEL.md` in both repos at Phase 0 exit. Captured here for context.

| Actor | Goal | Primary mitigation | Defense-in-depth |
|---|---|---|---|
| **Malicious customer** | Reach another customer's agent / desktop | Session-derived routing + Postgres RLS | Per-VM token, capability tokens, adversarial CI tests |
| **Compromised customer agent** (prompt-injected) | Reach platform control plane from VM | VM egress allow-list (Cloudflare + LLM proxy + Supabase only) | Audit logs, no path from VM back into control plane |
| **Network attacker** sniffing tryopencomputer.com | Steal tokens / sessions | TLS everywhere, Supabase JWT in `HttpOnly` cookie, short-TTL capability tokens, no tokens in URLs | Token rotation |
| **Stolen DB snapshot** | Replay tokens to access VMs | Per-VM token rotated on each VM restart; tokens encrypted at rest with KMS | Tunnel auth is a separate factor (CF tunnel token) |
| **Compromised `oc-workspace` server** | Route any user → any VM (keystone compromise) | Hardening: minimal codebase, audited deps, separated from SaaS-shell deploy if possible | VM still checks pinned token tied to that VM |
| **Hetzner VM IP recycling** | New customer's VM inherits IP, stale row routes wrong user | Routing uses tunnel hostname, not IP — IP changes don't matter | RLS prevents stale rows being read |
| **Customer escapes their VM** | Reach platform via tunnel | Tunnel is unidirectional (VM dials out; platform calls in; VM can't reach platform internals) | Egress firewall, audit logs |

---

## 7. Security invariants (non-negotiables)

To live as `docs/SECURITY-INVARIANTS.md` in both repos.

| # | Invariant | Enforcement |
|---|---|---|
| 1 | No public listener on any user VM. Hetzner firewall drops all inbound. | `nmap` check in CI per VM build |
| 2 | No client-controlled tenant identifier. Tunnel target is a pure function of the verified Supabase session. | Lint rule + code review checklist |
| 3 | Postgres RLS on every tenant-scoped table BEFORE any real user. | CI fails if a table without RLS exists |
| 4 | Per-VM `OC_DASHBOARD_TOKEN` env-pinned and verified VM-side. | Test in `opencomputer` repo |
| 5 | Adversarial cross-tenant tests green in CI before each phase merges. | CI gate |
| 6 | No phase ships to production without the previous phase's exit criteria green. | This document tracks it |
| 7 | No real customer signups until third-party pentest passes. | Out-of-band — gated at DNS cutover |

---

## 8. Build phases

> Each phase has: **Goal · Deliverables · Verification · Exit criteria · Notes log.**
> Phases are sequential; later phases assume earlier ones are 🟢.

---

### Phase 0 — Foundations

**Status:** 🟡 In review (docs drafted; reviews pending)
**Repos:** `[both]`
**Blocked by:** —
**Estimated effort:** S (1-2 days, mostly writing)

**Goal:** Document the threat model + invariants + architecture before any code, so every later phase has a rubric.

#### Deliverables
- 🟢 `[both]` Write `docs/THREAT-MODEL.md` (in both repos — identical content, mirror it). Source: copy §6 of this doc; expand each row with concrete attack vector and proof-of-mitigation.
- 🟢 `[both]` Write `docs/SECURITY-INVARIANTS.md` (in both repos). Source: copy §7 of this doc; for each invariant add (a) what violates it, (b) how CI/lint enforces it, (c) what to do if it ever fails.
- 🟢 `[both]` Write `docs/ARCHITECTURE.md` (in both repos). Source: copy §5 diagram + a paragraph per box.
- 🟢 `[both]` Add a PR template (`.github/pull_request_template.md`) with checklist:
  - [x] Touches a tenant-scoped resource? If yes — RLS policy reviewed?
  - [x] Adds a new endpoint? If yes — does it accept a tenant id from the client? (must be NO)
  - [x] New tests added to `test_tenant_isolation`?
  - **Path note:** Template lives at REPO ROOT `.github/pull_request_template.md` (not inside `OpenComputer/.github/`) — GitHub picks templates up only from repo root. Decision logged in §9.
- 🟢 `[ocp]` Pointer file `docs/tryopencomputer-plan.md` containing the canonical-plan URL plus a brief index of which oc-platform components are referenced in which phases.

#### Verification
- ☐ All three docs reviewed by Archit + at least one other person (record names in Notes).
- ☐ PR template fires on a test PR.

#### Exit criteria
- 🟡 All deliverables 🟢 (drafts in PR; review pending)
- ☐ Plan §9 Decision Log has any new decisions appended

#### Notes / decisions log
<!-- Append one line per work session: YYYY-MM-DD: <what happened> -->
- 2026-05-18: Phase 0 docs drafted by Claude on branch `docs/phase-0-foundations-2026-05-18` in both repos. THREAT-MODEL.md expanded from 7 rows to 8 numbered actors (added Actor H — Insider) with explicit attack vectors and proof-of-mitigation per actor. SECURITY-INVARIANTS.md expanded each of the 7 invariants to a 4-section block (rule / violation / enforcement / response). ARCHITECTURE.md ported §5 diagram, added a paragraph per box, 3 data-flow walkthroughs (happy path, provisioning, rotation/destruction), responsibility matrix, and "what's deliberately NOT in this architecture." PR template at REPO ROOT `.github/pull_request_template.md` in both repos. oc-platform mirrors fix the relative `plans/` link to point at the canonical URL on github.com since the plan file does not live in oc-platform.

---

### Phase 1 — Foundational primitives

**Status:** 🟢 Both halves merged 2026-05-18 (OC #668, ocp #2)
**Repos:** `[oc]` + `[ocp]` (two independent PRs)
**Blocked by:** Phase 0 — 🟢 (OC #667 + ocp #1 merged 2026-05-18)
**Estimated effort:** S (half a day each)

**Goal:** Two surgical changes the whole architecture leans on — env-pinnable OC dashboard token + Postgres RLS on every tenant table.

#### 1a · `[oc]` OpenComputer: env-pinnable dashboard token

**File:** `OpenComputer/opencomputer/dashboard/server.py`

```python
# was:
_SESSION_TOKEN: str = secrets.token_urlsafe(32)

# becomes:
_SESSION_TOKEN: str = (
    os.environ.get("OC_DASHBOARD_TOKEN")
    or secrets.token_urlsafe(32)
)
```

- 🟢 `[oc]` Patch `dashboard/server.py` as above.
- 🟢 `[oc]` Add `tests/test_dashboard_token_env_override.py`:
  - With `OC_DASHBOARD_TOKEN=test-xyz` → `_SESSION_TOKEN == "test-xyz"` ✓
  - Without env var → token is a fresh `token_urlsafe(32)` (length 43) ✓
  - Reload-after-env-change: env at import time wins (documented) ✓
  - Plus: same-env-twice-yields-same-token (the production restart property) ✓
  - Plus: empty-string env falls through to random (no empty-Bearer accepted) ✓
  - Plus: app.state.session_token propagation ✓
  - **7 tests total, all green in CI.** PR #668 squash-merged 2026-05-18.
- 🟢 `[oc]` Update `docs/SECURITY-INVARIANTS.md` invariant #4 with the test-suite references and the post-Phase-1a violation example.
- 🟢 `[oc]` Open PR, link to this plan, merge, tag a new OC release. (Merged; tag is human-attended — see RELEASE.md.)
- 🟡 `[oc]` Bump OC version pin in `oc-platform`'s cloud-init template — folded into Phase 2a (in flight).

#### 1b · `[ocp]` Supabase RLS on every tenant-scoped table

Tables today: `users`, `payments`, `instance_events`, `snapshots`.
Tables added by this plan: `vm_tunnels`, `capability_tokens` (audit table, optional).

For **each** table:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON <table>
  FOR ALL
  USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid());

-- Service-role bypass for service-api's privileged ops:
CREATE POLICY service_role_bypass ON <table>
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);
```

- 🟢 `[ocp]` Hand-authored SQL migration `packages/service-api/drizzle/0001_enable_rls.sql` covering `users`, `payments`, `instance_events`, `snapshots`. Idempotent (DROP POLICY IF EXISTS + CREATE POLICY). Two policies per table: `<table>_tenant_isolation` (authenticated) + `<table>_service_role_bypass` (service_role).
- 🟢 `[ocp]` Column-name audit: see §9 decision (2026-05-18) — kept `user_id`, did NOT rename to `owner_id`. RLS policies join through `users.auth_id = auth.uid()::text` (the bridge to Supabase's `auth.users.id`).
- 🟢 `[ocp]` Test suite `packages/service-api/tests/test_rls.spec.ts` (vitest):
  - Anonymous client cannot SELECT (4 tests, one per tenant table). ✓
  - User A's JWT-authed client only sees A's rows; never B's (4 tests). ✓
  - Service-role bypass works (1 test). ✓
  - INSERT with wrong `user_id` while authed as A is rejected (1 test). ✓
  - UPDATE on B's row while authed as A is a no-op (1 test). ✓
  - Suite skips with a CLEAR message when `TEST_DATABASE_URL` + JWTs aren't provided (never silently passes).
- ☐ `[ocp]` Rip out any application-level ownership checks that are now redundant — deferred to a follow-up after RLS has been in prod for >7 days; ripping them out the same week we land RLS removes the belt-and-suspenders before we have confidence.
- 🟢 `[ocp]` Standalone CI gate `packages/service-api/scripts/check_rls.ts`. Asserts (a) every tenant-scoped table exists in pg_tables, (b) has rowsecurity=true, (c) has both required policies, (d) every public table is either in TENANT_TABLES or EXCLUDED_TABLES (catches schema drift). Wired as `npm run db:check:rls`.

#### Verification
- ☐ Boot `oc workspace backend` with `OC_DASHBOARD_TOKEN=test-xyz`; `curl` with that Bearer → 200; curl without → 401.
- ☐ `psql` as `anon`-role: every SELECT on `users` returns 0 rows.
- ☐ `psql` swapping to user A's JWT: SELECT on `users` returns only A's row.

#### Exit criteria
- 🟢 Both PRs merged — OC #668 + ocp #2 squash-merged 2026-05-18.
- 🟢 All tests green in CI — oc-side 21 / 21 green; ocp-side suite skips cleanly without TEST_DATABASE_URL (real-DB run is a manual gate against staging Supabase, done outside CI for now).
- 🟢 Decision §9 records: "RLS canonical column is `user_id` — see 2026-05-18 row"

#### Notes / decisions log
<!-- 2026-MM-DD: ... -->
- 2026-05-18: Phase 1a code change + 7-test suite drafted on branch `feat/phase-1a-env-pinned-dashboard-token`. New tests green; existing `test_dashboard_server.py` + `test_dashboard_fastapi.py` (14 tests) unchanged. SECURITY-INVARIANTS #4 updated to reference the new test file and the post-Phase-1a violation pattern. Phase 1b work started in parallel on `feat/phase-1b-rls` in oc-platform.
- 2026-05-18: Phase 1b shipped to a PR. Survey of `packages/service-api/src/db/schema.ts` showed existing column name is `user_id`, NOT `owner_id` (assumption in §10 Q2 was wrong). Decision in §9: kept `user_id`. RLS join goes through `users.auth_id = auth.uid()::text`. Migration is one hand-authored SQL file (idempotent), adversarial vitest suite (11 tests, skips cleanly without test DB env), standalone `check_rls.ts` CI gate. Mirror copy of SECURITY-INVARIANTS.md in oc-platform updated to match the OC-side Phase-1a edits.
- 2026-05-18: Both Phase 1 PRs squash-merged. CI for OC #668: 6/6 checks green (ruff, pytest 3.12, pytest 3.13, introspection × 3 OSes). Phase 1 status → 🟢. Phase 2 unblocked.

---

### Phase 2 — Reverse tunnel (VM no public ports)

**Status:** 🟡 2c shipped (scaffolding); 2a in flight; 2b pending operator action
**Repos:** `[ocp]` + `[ops]`
**Blocked by:** Phase 1 — 🟢 (merged 2026-05-18)
**Estimated effort:** M (2-4 days)

**Goal:** A newly-provisioned VM has zero inbound listeners. cloudflared dials out. Only Cloudflare → tunnel reaches it. No `--insecure`.

#### 2a · `[ocp]` Cloud-init rewrite

**File:** `oc-platform/templates/cloud-init.yaml.tmpl`

Changes:
- ☐ Replace `oc dashboard --host 0.0.0.0 --port 9119 --insecure` → `oc workspace backend --host 127.0.0.1 --port 9119` (uses post-#651 OC CLI). Add `Environment=OC_DASHBOARD_TOKEN={{OC_DASHBOARD_TOKEN}}` to the systemd unit.
- ☐ Change noVNC: `--listen 6080` → `--listen 127.0.0.1:6080`.
- ☐ Install `cloudflared` (apt or upstream pkg) in `packages:` list.
- ☐ Add `/etc/systemd/system/cloudflared.service`:
  ```yaml
  [Unit]
  After=network-online.target
  Wants=network-online.target
  [Service]
  Type=simple
  ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate run --token {{CF_TUNNEL_TOKEN}}
  Restart=always
  RestartSec=5
  [Install]
  WantedBy=multi-user.target
  ```
- ☐ In `runcmd:` — enable+start cloudflared *before* `oc-dashboard.service`.
- ☐ Remove the public Anthropic key env var; the agent will use the user's LLM-proxy creds instead (matches our auth decision — see §9).
- ☐ Tighten egress: nftables rule to allow outbound only to `*.cloudflare.com`, the user's LLM-proxy domain, and Supabase. Drop everything else.

#### 2b · `[ocp]` `service-api`: mint tunnel + token at provision time

**Files:**
- `oc-platform/packages/service-api/src/services/provisioner.ts` (new helpers)
- `oc-platform/packages/service-api/src/services/tunnels.ts` (new module)
- `oc-platform/packages/service-api/src/db/schema.ts` (new table)

Tasks:
- ☐ Add `vm_tunnels` table (Drizzle schema):
  ```ts
  export const vmTunnels = pgTable("vm_tunnels", {
    id: uuid("id").primaryKey().defaultRandom(),
    ownerId: uuid("owner_id").notNull().references(() => users.id),
    tunnelId: text("tunnel_id").notNull(),       // Cloudflare tunnel id
    hostname: text("hostname").notNull().unique(), // agent-<uuid>.oc-vms.tryopencomputer.com
    ocTokenEncrypted: bytea("oc_token_encrypted").notNull(),
    createdAt: timestamp("created_at").notNull().defaultNow(),
    revokedAt: timestamp("revoked_at"),
  });
  ```
- ☐ Enable RLS on `vm_tunnels` (Phase 1 pattern).
- ☐ `services/tunnels.ts`:
  - `createCloudflareTunnel(ownerId)` → calls CF API → returns `{ tunnelId, hostname, token }`.
  - `revokeCloudflareTunnel(tunnelId)` → revokes via CF API + sets `revoked_at`.
- ☐ `services/provisioner.ts`:
  - Generate `ocToken = randomBytes(32).toString("base64url")`.
  - Call `createCloudflareTunnel(user.id)`.
  - `db.insert(vmTunnels)` with encrypted `ocToken` (use libsodium or AWS KMS).
  - Render cloud-init with `{{CF_TUNNEL_TOKEN}}` and `{{OC_DASHBOARD_TOKEN}}` interpolated.
- ☐ On `DELETE /api/instance`: revoke the tunnel, mark `vm_tunnels.revoked_at`.
- ☐ Add tunnel cleanup to the existing scheduled cron (`cron.ts`).

#### 2c · `[ops]` Cloudflare setup (one-time)

**Status:** 🟡 runbook shipped (ocp #3 merged 2026-05-18); operator UI clicks in progress.

- 🟢 Document in `oc-platform/docs/cloudflare-setup.md` — 10-section runbook with positive AND negative verification curls.
- 🟢 Pre-wire `service-api`: `tunnels.ts` config + signatures + `smokeTestCloudflareAccess()` + `cf:smoke` npm script. Implementation deferred to Phase 2b.
- ☐ **Operator (Archit):** create CF account, register `tryopencomputer.com` via Cloudflare Registrar (~$10.44/yr), capture Account ID + Zone ID, mint API token with the 3 scopes (Tunnel Edit, DNS Edit scoped to zone, Zone Read), set 4 env vars, run `npm run cf:smoke` → OK.
- ☐ Confirm subdomain `oc-vms.tryopencomputer.com` resolves as expected once first tunnel is minted.

#### Verification
- ☐ Provision a test VM in staging. `nmap` its public IP from outside → 0 open ports.
- ☐ `curl http://<vm-ip>:9119` from outside → connection refused (firewall) or no route.
- ☐ `curl https://agent-<userid>.oc-vms.tryopencomputer.com/v1/health` with correct `Authorization: Bearer <token>` → 200.
- ☐ Same curl without the Bearer → 401 (OC token enforcement working).
- ☐ Kill `cloudflared` on the VM via `systemctl stop cloudflared`; verify hosted endpoint goes 502 within 5s; verify systemd restarts cloudflared within 5s.
- ☐ Delete the test VM via API; verify the tunnel hostname goes 404 (revoked).

#### Exit criteria
- ☐ All Verification items 🟢
- ☐ `nmap` check automated as a post-provision step in `oc-compute` (lease state `ready` only after `nmap` confirms 0 listening ports)
- ☐ Threat model row for "VM exposed publicly" marked mitigated

#### Notes / decisions log
- 2026-05-18: Phase 2c scaffolding shipped — runbook + env-var wiring + `tunnels.ts` stub + `cf:smoke` script (ocp #3). Operator clicks remain (account, domain registration, token). Subdomain root confirmed as `oc-vms` (plan default). Registrar decision: direct at Cloudflare Registrar (no third-party).

---

### Phase 3 — Capability tokens

**Status:** ☐ Not started
**Repos:** `[ocp]` + `[oc]`
**Blocked by:** Phase 1
**Estimated effort:** S (1-2 days)

**Goal:** When the frontend needs to refer to a resource, give it a short-lived signed handle, not a raw id. No `<userId>` in URLs anywhere.

#### Deliverables

- ☐ `[ocp]` `services/capabilities.ts`:
  ```ts
  type Capability = {
    sub: string;      // user id (must match session)
    res: "desktop" | "chat-stream";  // resource kind
    res_id?: string;  // opaque, internal only
    iat: number;
    exp: number;      // 5min for desktop, 1min for chat-stream
  };
  export function mintCapability(c: Capability): string;
  export function verifyCapability(token: string, expected: { sub: string; res: string }): Capability;
  ```
- ☐ `[ocp]` Replace `GET /api/instance/desktop` to return `{ wsUrl, capability }` — the URL is the tunnel hostname; the capability proves "this user is allowed to open the desktop WS now."
- ☐ `[ocp]` Audit every endpoint that today accepts a user-influenced id from the body or URL. Replace each with capability or session-derived. List in §10 Open Questions if any can't be converted.
- ☐ `[ocp]` Test suite `tests/test_capabilities.spec.ts`:
  - Forged signature → reject.
  - Expired → reject.
  - Tampered claims → reject.
  - Cross-user (A's capability used by B's session) → reject.
  - Resource swap (`desktop` token used at a `chat-stream` endpoint) → reject.
- ☐ `[oc]` `oc-workspace`'s BFF endpoints that consume capabilities call `verifyCapability(token, { sub: session.user.id, res: "desktop" })` — never derive `sub` from the capability.

#### Exit criteria
- ☐ No endpoint accepts a raw resource id from the client
- ☐ `test_capabilities.spec.ts` green
- ☐ Lint rule: any new endpoint with a `:userId` path param fails CI

#### Notes / decisions log

---

### Phase 4 — `oc-workspace` becomes the BFF + proxy

**Status:** ☐ Not started
**Repos:** `[oc]` (primary) + small `[ocp]` glue
**Blocked by:** Phases 1, 2, 3
**Estimated effort:** L (1-2 weeks)

**Goal:** Port `oc-platform`'s Next.js pages into `oc-workspace`'s TanStack/Vite app. Make `oc-workspace`'s Node server a per-user reverse proxy.

#### 4a · Port pages (Next.js → TanStack/Vite)

Map of pages:

| `oc-platform` (Next.js) | `oc-workspace` (TanStack) | Notes |
|---|---|---|
| `app/page.tsx` (landing) | `src/routes/index.tsx` | Marketing copy + CTA |
| `app/sign-in/page.tsx` | `src/routes/sign-in.tsx` | Supabase auth UI |
| `app/auth/callback/route.ts` | `src/routes/auth/callback.tsx` | OAuth callback |
| `app/dashboard/page.tsx` | `src/routes/app/index.tsx` | Agent dashboard (deploy/stop/status, provisioning checklist) |
| `app/dashboard/desktop/page.tsx` | `src/routes/app/desktop.tsx` | noVNC viewer |
| `app/dashboard/billing/page.tsx` | `src/routes/app/billing.tsx` | Razorpay checkout |
| `app/dashboard/settings/page.tsx` | `src/routes/app/settings.tsx` | Profile + agent config |

Shared lib (port from `oc-platform/packages/frontend/src/lib/`):
- `[oc]` `oc-workspace/src/lib/supabase.ts`
- `[oc]` `oc-workspace/src/lib/session.ts` (server-side user resolution)
- `[oc]` `oc-workspace/src/lib/api.ts` (typed wrappers to service-api)

Tasks:
- ☐ `[oc]` Add Supabase JS SDK to `oc-workspace` deps.
- ☐ `[oc]` Port each page (1 PR per page is reasonable). For each, replicate the data flow but using TanStack Query + the typed `api.ts` instead of raw `fetch`.
- ☐ `[oc]` Move shadcn-style components / tailwind classes — assess overlap with hermes-workspace's existing components; reuse where possible.
- ☐ `[oc]` Add `OAuth` redirect URL `tryopencomputer.com/auth/callback` to Supabase project (allowed redirects).

#### 4b · BFF endpoints in `oc-workspace`'s Node server

**Files:** `oc-workspace/src/server/` (new files; this directory exists)

| Path | Method | Behavior |
|---|---|---|
| `/api/me` | GET | Verify Supabase JWT → return current user (no DB call needed) |
| `/api/agent/proxy/*` | ANY | **The single dynamic proxy.** See pseudocode below. Streams SSE; preserves `content-type`; strips client `Authorization`/`Cookie`. |
| `/api/agent/desktop` | GET (Upgrade) | Verify session + `desktop` capability → upgrade to WS → bridge to `wss://<tunnel>/desktop` |
| `/api/instance/*` | ANY | Forward Supabase JWT to `service-api` (control plane passthrough) |

**The proxy — `oc-workspace/src/server/agentProxy.ts`:**

```ts
export async function proxyToUserAgent(req: Request, res: Response) {
  // 1. ONE source of truth for the user
  const user = await verifySupabaseJwt(req);  // throws → 401
  if (!user) return res.status(401).end();

  // 2. Resolve tunnel — purely from verified user, NO request input
  const tunnel = await db.query.vmTunnels.findFirst({
    where: and(eq(vmTunnels.ownerId, user.id), isNull(vmTunnels.revokedAt)),
  });
  // RLS protects this; the .where() is belt-and-suspenders.
  if (!tunnel) return res.status(404).json({ error: "no agent" });

  // 3. Strip anything client may have spoofed
  const headers = sanitizeHeaders(req.headers); // drops Authorization, Cookie, X-Forwarded-*, Host
  headers["Authorization"] = `Bearer ${decrypt(tunnel.ocTokenEncrypted)}`;

  // 4. Stream-forward
  const upstream = await fetch(`https://${tunnel.hostname}${req.url}`, {
    method: req.method,
    headers,
    body: req.body,
    duplex: "half",
  });
  return pipeStream(upstream, res);
}
```

Tasks:
- ☐ `[oc]` Implement `verifySupabaseJwt` (verify against Supabase JWKS, cache JWKS for an hour).
- ☐ `[oc]` Implement `sanitizeHeaders` (allow-list, not deny-list).
- ☐ `[oc]` Implement `pipeStream` with SSE awareness (no buffering on `text/event-stream`).
- ☐ `[oc]` Implement WebSocket proxy for `/api/agent/desktop` (likely using `ws` or `http-proxy-3`).
- ☐ `[oc]` Wire-up: `oc-workspace/src/server/index.ts` mounts the new BFF routes alongside existing.
- ☐ `[oc]` `oc-workspace` reads `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SERVICE_API_URL`, plus DB conn (or call service-api for the tunnel lookup instead of going direct — see §10).

#### 4c · `[oc]` Tenant isolation test suite — `oc-workspace/tests/test_tenant_isolation.spec.ts`

**This is the single most important test file in the codebase.**

Minimum 20 cases. Examples:

- ☐ A's session + B's tunnel hostname in `Host:` → must reach A's agent regardless.
- ☐ A's session + body containing `vm_id: B's` → must reach A's agent.
- ☐ A smuggles `Authorization: Bearer <B's OC token>` → header must be stripped, A's token used.
- ☐ A uses B's capability token → reject.
- ☐ Expired Supabase JWT → reject.
- ☐ JWT signed by wrong key → reject.
- ☐ WebSocket desktop: A's capability cannot open B's desktop WS.
- ☐ A's request after A's VM deleted → 404 (not someone else's stale row).
- ☐ A's request after IP recycling (A deleted, B got new VM with same IP) → A's request 404 / B's 200, no crossover.
- ☐ Race: A's deploy-then-delete-then-deploy → only the current tunnel wins.
- ☐ Property-based fuzzing: random JWTs × random paths × random capabilities — no 2xx for foreign resources.

#### Exit criteria
- ☐ All ported pages render with no console errors in staging
- ☐ Chat works end-to-end through the proxy (SSE streams cleanly)
- ☐ Desktop works end-to-end through the WS proxy
- ☐ `test_tenant_isolation` has ≥ 20 cases, all 🟢
- ☐ CI gate: any new endpoint requires a matching adversary test in the same PR

#### Notes / decisions log

---

### Phase 5 — VM-side defense layers

**Status:** ☐ Not started
**Repos:** `[oc]` + `[ocp]`
**Blocked by:** Phase 2 + Phase 4
**Estimated effort:** S

**Goal:** Even if all upstream layers are wrong, the VM rejects requests not from its tenant.

- ☐ `[oc]` Confirm `oc workspace backend` rejects any `/v1/*` or `/api/*` without `Authorization: Bearer <pinned token>` → 401. (Already does — add explicit test.)
- ☐ `[ocp]` Add VM-side audit log: every authenticated request logged to `/var/log/oc-audit.log` with `(ts, path, body-hash, response-code)`. Rotated by logrotate. Shipped to platform via existing heartbeat extension.
- ☐ `[ocp]` Egress allow-list (nftables, set in cloud-init): only `*.cloudflare.com`, the LLM-proxy domain, Supabase, and Hetzner metadata. Drop everything else outbound.
- ☐ `[ocp]` Decision: do we want SSH inbound for admin? If yes, only from a fixed admin IP. If no, use Hetzner web console only. Record decision in §9.

#### Exit criteria
- ☐ A request with wrong token to the tunnel → 401 + audit entry visible at platform within 60s
- ☐ A test agent attempting `curl https://example.com` from inside the VM is blocked (egress rule works)

#### Notes / decisions log

---

### Phase 6 — Operational hardening

**Status:** ☐ Not started
**Repos:** `[ops]` + small touches in `[oc]` and `[ocp]`
**Blocked by:** Phases 4 + 5
**Estimated effort:** M

**Goal:** Detect, alert on, and respond to anything we missed.

- ☐ `[ops]` Centralized logging — pick one: Loki + Grafana / Datadog / Cloudflare Logs. Aggregate: oc-workspace BFF, service-api, VM audit logs.
- ☐ `[ops]` Alerts (page someone):
  - 401 rate > N/min
  - Any RLS policy violation logged
  - Tunnel-not-found 404 for a known-active user
  - Header-sanitizer ever stripped an `Authorization` from a client request
- ☐ `[oc]` Rate limiting at oc-workspace BFF, per Supabase user: chat completions, deploy attempts, capability mints.
- ☐ `[ocp]` Token rotation: `OC_DASHBOARD_TOKEN` rotates on every VM restart (cloud-init regenerates; service-api refreshes DB row via heartbeat). Cloudflare tunnel token rotates on 30-day schedule.
- ☐ `[ocp]` Kill switch: admin endpoint `POST /admin/users/:id/quarantine` → revoke tunnel + invalidate Supabase session + suspend VM. Tested in staging.
- ☐ `[ops]` Secrets at rest — all DB token columns encrypted with KMS key. Backups encrypted. Document key rotation procedure.
- ☐ `[ops]` Runbooks in `docs/runbooks/`:
  - `incident-cross-tenant-suspected.md`
  - `incident-tunnel-down.md`
  - `incident-token-leak.md`
  - `routine-token-rotation.md`

#### Exit criteria
- ☐ Synthetic cross-tenant attempt in staging pages within 60s
- ☐ Runbooks exist for every alert
- ☐ Kill-switch tested in staging

---

### Phase 7 — External validation

**Status:** ☐ Not started
**Repos:** —
**Blocked by:** Phases 1-6
**Estimated effort:** 2-6 weeks (waiting on vendors)

**This phase is non-negotiable. No customer signs up before this completes.**

- ☐ Internal red team (1 week): Archit + at least 1 other attempts to break tenant isolation. Document in `docs/red-team-2026-MM.md`.
- ☐ Engage a reputable pentest firm (e.g. Trail of Bits, Doyensec, NCC Group). Scope: tenant isolation, tunnel auth, capability tokens, VM egress, billing.
- ☐ Budget at least 2 rounds (initial + retest after fixes).
- ☐ Stand up a private bug bounty (HackerOne / Bugcrowd) for trusted researchers before launch.
- ☐ Address every high/critical finding before customers.

#### Exit criteria
- ☐ Pentest report has no unfixed high-severity findings
- ☐ Bug bounty program live

---

### Phase 8 — Cutover

**Status:** ☐ Not started
**Blocked by:** Phase 7
**Estimated effort:** S (mostly waiting)

- ☐ Staging environment runs production config for 2 weeks with synthetic traffic
- ☐ Final adversarial test sweep + dep audit (`npm audit`, `pip-audit`, `ruff`, `mypy --strict`)
- ☐ DNS cutover: `tryopencomputer.com` → oc-workspace deployment
- ☐ First 10 customers are personal-network only
- ☐ Monitor 2 weeks; if clean → open public signups
- ☐ Sunset `oc-platform/packages/frontend` Next.js (keep around as ref for 90 days, then delete)

---

## 9. Decision log

> Append decisions here as they're made. Newest first. Include date + who.

| Date | Decision | Why | Decided by |
|---|---|---|---|
| 2026-05-18 | `tryopencomputer.com` = `oc-workspace`, not a new third frontend | Avoid yet-another-app; oc-workspace already has a Node SSR shell to extend | Archit |
| 2026-05-18 | Cloudflare Tunnel for MVP reverse-tunnel (vs Tailscale / WireGuard) | Lowest-effort, free at this scale, per-VM revocable tokens, easy migration path | Archit |
| 2026-05-18 | Agent runs as `root` on the VM | Agent owning its computer is the product; root inside an isolated single-tenant VM is acceptable, the boundary is the VM's tunnel | Archit |
| 2026-05-18 | LLM keys via user's own proxy + their JWT; not baked into cloud-init | Closes the "secret in metadata service" hole | Archit |
| 2026-05-18 | Postgres RLS is the storage-level invariant; application checks are belt-and-suspenders | Single mistake should not be a breach; RLS is the structural floor | Archit |
| 2026-05-18 | `service-api` stays separate on AWS; `oc-workspace`'s BFF talks to it | service-api is already deployed and stable; don't fold control plane into the frontend | Archit |
| 2026-05-18 | Next.js → TanStack/Vite port (not keep Next.js as shell) | One framework end-to-end is cleaner than maintaining two | Archit |
| 2026-05-18 | PR template lives at REPO ROOT `.github/pull_request_template.md` (not `OpenComputer/.github/`) | GitHub only picks up PR templates from repo root; subdirs are ignored. Plan §8 Phase 0 said `OpenComputer/.github/` — superseded by this entry. | Phase 0 prep |
| 2026-05-18 | THREAT-MODEL adds Actor H (Insider / developer with prod access) | Modeling only external actors leaves an obvious gap; least-privilege + audit trail mitigations are concrete enough to commit to. | Phase 0 prep |
| 2026-05-18 | RLS canonical column stays `user_id` (NOT renamed to `owner_id`). Resolves §10 Q2. | The existing schema already uses `user_id` referencing local `users.id`; the bridge to Supabase auth is `users.auth_id`. RLS policies on child tables join through `users.auth_id = auth.uid()::text`. Renaming would touch 4 tables + ~20 query sites for ZERO structural gain — the auth_id join is required either way. | Phase 1b |
| 2026-05-18 | Application-level ownership checks are NOT ripped out in the Phase 1b PR; deferred ≥ 7 days after RLS lands in prod. | Removing the app checks the same week we land RLS removes the belt-and-suspenders before we have confidence the structural floor holds. Schedule the cleanup PR once we have ≥ 1 week of RLS-in-prod with zero policy-violation logs. | Phase 1b |
| 2026-05-18 | Subdomain root for per-VM tunnels stays `oc-vms.tryopencomputer.com` (plan default confirmed). | Matches plan + threat model + architecture docs. Each VM lands at `agent-<uuid>.oc-vms.tryopencomputer.com`. | Phase 2c |
| 2026-05-18 | Register `tryopencomputer.com` directly via Cloudflare Registrar (not through Porkbun / Namecheap / GoDaddy). | Same account as DNS — no nameserver pointing, no transfer dance, $10.44/yr at-cost. One fewer account to rotate / maintain. | Phase 2c |
| 2026-05-18 | Cloudflare API token scoped to EXACTLY 3 permissions (Tunnel Edit, DNS Edit @ zone, Zone Read @ zone). | Least-privilege: compromise of service-api can mint tunnels on our zone (annoying, contained) but cannot pivot to R2 / Workers / other zones. Negative-curl test in runbook §6 verifies the boundary. | Phase 2c |

---

## 10. Open questions

> Anything that needs answering before / during the relevant phase. Resolve into §9 once decided.

| # | Question | Blocks phase | Status |
|---|---|---|---|
| 1 | Does `oc-workspace`'s BFF talk to Supabase DB directly, or always proxy tunnel-lookup through service-api? | 4 | Open. Direct = lower latency. Via service-api = single auth path. Lean: via service-api. |
| 2 | Standardize column name `userId` vs `owner_id` across `service-api` DB? | 1 | **Resolved 2026-05-18** — kept `user_id`. See §9. |
| 3 | Allow SSH inbound to VMs for admin (from a fixed admin IP), or rely only on Hetzner web console? | 5 | Open. |
| 4 | Where to host `oc-workspace` (Vercel / Fly.io / Render / self-hosted)? Affects SSE + WS support. | 4 | Open. Lean: Fly.io — full WS + SSE support, edge-deploy possible. |
| 5 | Snapshot retention/restore — port from `oc-platform`'s existing scaffolding or rebuild? | 6 | Open. |
| 6 | Billing — Razorpay subscription model stays as-is, or move to subscriptions API (recurring)? | 4 | Open. |
| 7 | Cloudflare Zero Trust Access policies layered on tunnels — yes/no? Adds another auth layer. | 6 | Open. |

---

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation | Status |
|---|---|---|---|---|
| Tenant-isolation bug in BFF proxy | M | Critical | 5-layer defense, adversarial tests in CI, pentest | Phase 4 + 7 |
| Compromise of `oc-workspace` server (the keystone) | L | Critical | Minimize codebase, audit deps, separate deploy from public site | Phase 6 |
| Cloudflare outage takes all VMs offline | L | High | Tunnels are stateless; can migrate to Tailscale; staging tests | Phase 8 |
| Supabase compromise (identity provider) | L | Critical | Out of our hands; pick a battle-hardened provider; ToS/incident contract | Accepted risk |
| Agent prompt-injection reaches platform internals | M | High | VM egress allow-list; tunnel is unidirectional | Phase 5 |
| Hetzner VM IP recycling causes stale routing | L | High | Routing by tunnel hostname not IP; revoke tunnels atomically with VM delete | Phase 2 |
| Cloud-init secrets leak via metadata service | M | High | No long-lived secrets in cloud-init; tokens are short-lived + revocable | Phase 2 |
| Phase 1's OC token change breaks an existing OC deployment | L | M | Default falls back to random — opt-in only when env set | Phase 1 |
| DB snapshot leaked | L | Critical | KMS encryption of token columns; tunnel auth is independent second factor | Phase 6 |
| Cost overrun (Hetzner / Cloudflare / Supabase) | M | M | Per-tier budget alerts; suspended VMs free after 3-day grace | Phase 6 |

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **BFF** | Backend-for-frontend — `oc-workspace`'s Node SSR server doing per-user proxying |
| **Capability token** | Short-lived signed JWT proving "this user can access this specific resource right now" |
| **Cloudflared** | The Cloudflare Tunnel agent that runs on the VM and dials out |
| **JWKS** | JSON Web Key Set — Supabase's public-key endpoint used to verify JWTs |
| **Lease** | An `oc-compute` term for a VM allocation (lifecycle: pending → active → suspended → released) |
| **OC token** | The pinned `OC_DASHBOARD_TOKEN` env var on each VM; gates the OC backend |
| **RLS** | Row-Level Security — Postgres policies enforced at the storage engine, below application code |
| **Service-role** | A Supabase role that bypasses RLS — only `service-api` uses it, with caution |
| **Tunnel hostname** | `agent-<userId>.oc-vms.tryopencomputer.com` — the public-facing Cloudflare address |

---

## 13. Pre-launch final checklist

Before flipping `tryopencomputer.com` DNS to public:

- ☐ All 7 security invariants verified live in staging
- ☐ `nmap` of every staging VM shows 0 inbound ports
- ☐ Postgres RLS on every tenant-scoped table; tested with wrong-user attacks
- ☐ `test_tenant_isolation` ≥ 20 cases, all 🟢 in CI
- ☐ Capability tokens: cross-user replay rejected
- ☐ VM egress allow-list active; agent in staging cannot reach `service-api`'s IP
- ☐ Token rotation works end-to-end (kill VM → new VM gets new token → old token rejected)
- ☐ Kill-switch works (revoke user → all their requests 401 within 5s)
- ☐ Audit logs ship; alerts page within 60s of synthetic incident
- ☐ Backups encrypted; restore tested
- ☐ Third-party pentest passed (no unfixed high-severity)
- ☐ Runbooks written for every alert
- ☐ Legal: ToS, Privacy Policy, security disclosure email live
- ☐ First 10 customers are warm-network only

---

## Appendix A — File-level inventory

> Quick lookup of every file this plan touches. Use this when picking up cold.

### `[oc]` opencomputer
- `OpenComputer/opencomputer/dashboard/server.py` — env-pinnable token (Phase 1a)
- `OpenComputer/tests/test_dashboard_token_env_override.py` — new (Phase 1a)
- `OpenComputer/oc-workspace/src/routes/index.tsx` — landing (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/sign-in.tsx` — auth (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/auth/callback.tsx` — OAuth callback (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/app/index.tsx` — dashboard (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/app/desktop.tsx` — noVNC (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/app/billing.tsx` — Razorpay (Phase 4a)
- `OpenComputer/oc-workspace/src/routes/app/settings.tsx` — settings (Phase 4a)
- `OpenComputer/oc-workspace/src/lib/supabase.ts` — new (Phase 4a)
- `OpenComputer/oc-workspace/src/lib/session.ts` — new (Phase 4a)
- `OpenComputer/oc-workspace/src/lib/api.ts` — new (Phase 4a)
- `OpenComputer/oc-workspace/src/server/agentProxy.ts` — new (Phase 4b)
- `OpenComputer/oc-workspace/src/server/sanitizeHeaders.ts` — new (Phase 4b)
- `OpenComputer/oc-workspace/src/server/verifyJwt.ts` — new (Phase 4b)
- `OpenComputer/oc-workspace/src/server/wsDesktopProxy.ts` — new (Phase 4b)
- `OpenComputer/oc-workspace/tests/test_tenant_isolation.spec.ts` — new (Phase 4c) — **critical**
- `OpenComputer/docs/THREAT-MODEL.md` — new (Phase 0)
- `OpenComputer/docs/SECURITY-INVARIANTS.md` — new (Phase 0)
- `OpenComputer/docs/ARCHITECTURE.md` — new (Phase 0)
- `OpenComputer/.github/pull_request_template.md` — new (Phase 0)

### `[ocp]` oc-platform
- `packages/service-api/src/db/schema.ts` — add `vmTunnels` (Phase 2b); add `owner_id` rename (Phase 1b)
- `packages/service-api/drizzle/migrations/NNN_enable_rls.sql` — RLS on all tables (Phase 1b)
- `packages/service-api/drizzle/migrations/NNN_vm_tunnels.sql` — new table (Phase 2b)
- `packages/service-api/src/services/provisioner.ts` — mint tunnel + token (Phase 2b)
- `packages/service-api/src/services/tunnels.ts` — new (Phase 2b)
- `packages/service-api/src/services/capabilities.ts` — new (Phase 3)
- `packages/service-api/tests/test_rls.spec.ts` — new (Phase 1b)
- `packages/service-api/tests/test_capabilities.spec.ts` — new (Phase 3)
- `templates/cloud-init.yaml.tmpl` — rewrite (Phase 2a)
- `docs/THREAT-MODEL.md` — mirror (Phase 0)
- `docs/SECURITY-INVARIANTS.md` — mirror (Phase 0)
- `docs/ARCHITECTURE.md` — mirror (Phase 0)
- `docs/tryopencomputer-plan.md` — pointer (Phase 0)
- `docs/cloudflare-setup.md` — new (Phase 2c)
- `docs/runbooks/*.md` — new (Phase 6)
- `.github/pull_request_template.md` — new (Phase 0)

---

## Appendix B — Sources & references

- GitHub Codespaces — Security: <https://docs.github.com/en/codespaces/reference/security-in-github-codespaces>
- GitHub Codespaces — Deep dive: <https://docs.github.com/en/codespaces/about-codespaces/deep-dive>
- Cloudflare Tunnel docs: <https://developers.cloudflare.com/tunnel/>
- Tailscale Funnel: <https://intellizu.com/articles/cloudflare-tunnel-vs-tailscale/>
- Coder vs Codespaces vs Gitpod architecture: <https://www.vcluster.com/blog/comparing-coder-vs-codespaces-vs-gitpod-vs-devpod>
- Cloudflare Zero Trust: <https://blog.frankel.ch/cloudflare-zero-trust-tailscale/>
- Multi-tenant namespace tunnels: <https://medium.com/@instatunnel/consolidating-your-pipeline-implementing-multi-tenant-namespace-tunnels-6a821509ce56>

---

## Appendix C — Session origin

This plan was produced in a planning conversation on **2026-05-18**. Prior session work that landed before this plan started:

- PR **#651** — `chore: CLI surface cleanup — remove dead oc webui, fold oc dashboard into oc workspace backend` (merged to `main`).
- PR **#653** — `fix(computer-use): cua-driver 0.1.9 reconciliation + audit-loop hardening` (merged to `main`).

The plan was validated against:
- Reading `oc-platform`'s `README.md`, `CLAUDE.md`, `packages/frontend/src/app/dashboard/page.tsx`, `packages/service-api/src/routes/instance.ts`, `templates/cloud-init.yaml.tmpl`, `packages/service-api/src/services/{compute,vnc,provisioner}.ts`.
- Reading `opencomputer`'s `dashboard/server.py`, `cli_workspace.py`, `workspace/launcher.py`, `workspace/lifecycle.py`.
- WebSearch on reverse-tunnel SaaS architecture patterns (Codespaces, Coder, Gitpod, Cloudflare Tunnel, Tailscale).

---

**End of plan. Update statuses + decision log every session.**
