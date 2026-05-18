# tryopencomputer.com — Architecture (target state)

> **Status:** Phase 0 deliverable. Living architecture document.
> **Mirrors:** `oc-platform/docs/ARCHITECTURE.md` (identical content; update both together).
> **Canonical plan:** [`docs/plans/tryopencomputer-platform-build-2026-05-18.md`](plans/tryopencomputer-platform-build-2026-05-18.md)
> **Threat model:** [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
> **Invariants:** [`docs/SECURITY-INVARIANTS.md`](SECURITY-INVARIANTS.md)
> **Last updated:** 2026-05-18

This describes the **target** architecture — the system as it will look at the end of Phase 8. The current production state is summarized in plan §2 ("where we're starting from"). Phase 1 through Phase 8 in the plan are the transitions between current and target.

---

## 1. Topology

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

---

## 2. Component walk-through (one paragraph per box)

### Browser

The user's browser runs the `oc-workspace` SPA. Authentication state is a Supabase-issued JWT stored in an `HttpOnly` + `Secure` + `SameSite=Lax` cookie scoped to `tryopencomputer.com`. JavaScript on the page never sees the JWT; every privileged action goes through the same-origin BFF. Resource references (VM ids, file ids, session ids) the page holds are **signed capability tokens** issued by the BFF, scoped to `(user_id, resource_id, ttl ≤ 5 min)` — opaque from the client's perspective and unforgeable.

### `oc-workspace` (TanStack/Vite + Node BFF) — `tryopencomputer.com`

Hosted on Vercel/Fly/Render (decision in plan §9 deferred until Phase 6 — current lean: Fly). Serves the SPA as static assets plus a thin Node SSR layer. Its **BFF responsibilities** are:

1. Verify the Supabase JWT on every privileged request (JWKS-cached, ~1 RTT/hour).
2. Resolve `sub → user_id → vms.tunnel_hostname` via a single server-side Postgres query (under RLS, with `auth.uid()` set from the verified JWT).
3. Mint capability tokens for resources the SPA needs to address.
4. Proxy **SSE** (chat streams) and **WebSocket** (noVNC) traffic to the per-user Cloudflare tunnel, attaching the per-VM Bearer (`OC_DASHBOARD_TOKEN`) server-side.
5. Inject the user's LLM-proxy JWT into every chat request the VM makes through the proxy (decision in plan §9: secrets never live on the VM).

This is the keystone (THREAT-MODEL Actor E). Its codebase is minimal by design; it never serves marketing or unaudited UI features.

### Supabase (Auth + Postgres + RLS)

- **Auth** — JWT issuer; JWKS keys cached at the BFF. Hardware-MFA gated for admin access.
- **Postgres** — primary tenant store. Every tenant-scoped table has Row-Level Security enabled with policies keyed on `auth.uid()`. The BFF talks to Postgres using an **anon JWT in user context** (so RLS applies) for user actions, and using a **service-role** key only from `server/admin/*` files (so RLS bypass is localized and audit-able).
- **Migrations** — Drizzle in `oc-platform/packages/db/`. Every new table that holds user data must ship its RLS policies in the same migration; CI gates this.

Tables of interest (final schema):

| Table | Owner column | RLS | Notes |
|---|---|---|---|
| `users` | `id` | ✓ | Standard Supabase auth shape |
| `vms` | `owner_id` | ✓ | `tunnel_hostname`, `vm_token` (KMS-encrypted), `status`, `region`, `image_version` |
| `sessions` | `user_id` | ✓ | OpenComputer chat sessions (proxied through the BFF) |
| `billing_subscriptions` | `user_id` | ✓ | Razorpay subscription state |
| `audit_log` | `user_id` | ✓ | Cross-tenant routing decisions, admin actions |

### `service-api` (AWS) — billing + lifecycle

Hono server on AWS Lambda + RDS Postgres (or the same Supabase Postgres — decision in plan §9 deferred). Owns the **server-side authorities** that the BFF and Worker cannot:

- Razorpay webhook handler (subscription start/cancel/grace).
- VM lifecycle orchestration: dispatches to `oc-compute` to provision/destroy.
- **Mints** the Cloudflare tunnel for a new VM and the per-VM `OC_DASHBOARD_TOKEN`. Writes both into the `vms` row atomically (KMS-encrypted), then triggers the cloud-init injection.
- Rotates `OC_DASHBOARD_TOKEN` on VM restart, on suspicious activity, on schedule (Phase 6 deliverable).
- Holds the long-lived secrets — Razorpay, Cloudflare API token, Hetzner token, KMS grant. The BFF and VMs never see these.

The service is "control-plane" — it owns slow, infrequent state changes. Hot-path proxying is the BFF's job.

### Cloudflare (tunnel ingress)

Each user's VM has a dedicated tunnel `agent-<uuid>.oc-vms.example.com`. Cloudflare authenticates the connection from the VM's `cloudflared` daemon using a tunnel token (CF-issued, server-side). Inbound traffic from the BFF reaches Cloudflare's edge, which forwards through the tunnel to the VM's `127.0.0.1:9119` (and `:6080` for noVNC). Cloudflare WAF runs in front; rate limits at this layer are configured per-tunnel (Phase 6).

Why Cloudflare tunnel and not WireGuard / Tailscale / SSH reverse port forwarding:

- **Zero inbound ports on VM** — `cloudflared` only dials out. The Hetzner firewall can drop everything inbound.
- **Identity is the tunnel UUID, not the VM IP** — Hetzner IP recycling (THREAT-MODEL Actor F) becomes irrelevant.
- **Operationally simple** — no peer key distribution, no NAT traversal.

### Hetzner VM (per user)

One Hetzner Cloud VM per paying user (sized `cx23` or `cpx22` initially). Provisioned via `oc-compute` Worker → Hetzner Cloud API.

Components running on the VM:

- **`cloudflared`** (systemd unit) — dials OUT to Cloudflare with the CF-issued tunnel token; serves the inbound side of the tunnel onto loopback ports.
- **`oc workspace backend`** (systemd unit, runs as root) — binds `127.0.0.1:9119`, verifies inbound Bearer against the env-pinned `OC_DASHBOARD_TOKEN`. Runs the OpenComputer agent loop, plugin fleet, and HTTP backend. Root because the agent needs full computer access — see THREAT-MODEL Actor B for how we contain a compromised agent rather than refuse to give it the access it needs.
- **`oc gateway`** (systemd unit, optional) — channel daemon for Telegram/Discord/Slack/etc.; absent in the default per-user image, opt-in.
- **noVNC + TigerVNC** (systemd unit) — provides browser-accessible desktop, binding `127.0.0.1:6080`. Browser-side connects via the BFF's WebSocket proxy.
- **Hetzner Cloud firewall** + **nftables on the VM** — both enforce: ALL inbound DROPPED on the public interface; egress restricted to the allow-list (LLM proxy, Supabase, Cloudflare, OS package mirrors).

Provisioned by **cloud-init template** in `oc-platform/templates/cloud-init.yaml.tmpl`. The template is the audit boundary — what gets written here is what runs on every user's machine.

### `oc-compute` (Cloudflare Worker)

Thin proxy in front of the Hetzner Cloud API. Holds the Hetzner token in CF Worker secrets; never exposes it. Called only by `service-api` with a signed short-TTL request. Responsibilities:

- Create VM (with cloud-init payload baked from `oc-platform/templates/cloud-init.yaml.tmpl`).
- Destroy VM.
- List / status — for admin and the user's own dashboard (proxied through the BFF).

Separated from `service-api` so the Hetzner credential lives in a single tightly-scoped place (the Worker), not in the much larger AWS surface.

---

## 3. Data flow — happy path

**Scenario:** logged-in user opens `tryopencomputer.com/app`, types a message into their chat.

1. Browser sends `POST /api/chat/stream` with the Supabase cookie. Body: `{message: "..."}`. No tenant id.
2. BFF verifies the JWT (JWKS-cached).
3. BFF queries Postgres under user context: `SELECT tunnel_hostname FROM vms WHERE owner_id = auth.uid() AND is_active = true LIMIT 1`. RLS asserts `owner_id = auth.uid()`.
4. BFF retrieves the per-VM Bearer from `vms.vm_token` (KMS-decrypted at request time, not cached on disk).
5. BFF opens an SSE stream to `https://agent-<uuid>.oc-vms.example.com/v1/chat/completions` with `Authorization: Bearer <vm_token>`.
6. Cloudflare routes the request through the tunnel to the VM's loopback `:9119`.
7. `oc workspace backend` verifies the Bearer matches its env-pinned `OC_DASHBOARD_TOKEN`; if so, hands the request to the agent loop.
8. Agent loop calls its LLM provider via our LLM proxy, using a separate JWT minted by `service-api` and rotated regularly (never the platform's Anthropic API key).
9. Tokens stream back: agent → BFF → browser, with the BFF acting as a transparent pipe.

**No client-supplied identifier was consulted at any step.** Invariant 2 holds.

---

## 4. Data flow — VM provisioning

**Scenario:** user signs up, picks a plan, gets a VM.

1. Razorpay subscription webhook → `service-api`.
2. `service-api` generates a new `OC_DASHBOARD_TOKEN` (KMS-encrypt for storage, plaintext for VM injection).
3. `service-api` calls Cloudflare API to create a new tunnel; receives tunnel UUID + CF-issued tunnel token.
4. `service-api` calls `oc-compute` Worker with: `(user_id, region, image_version, cloud-init-payload)`.
5. Cloud-init payload contains: pinned `OC_DASHBOARD_TOKEN`, Cloudflare tunnel token, LLM-proxy JWT for that user.
6. `oc-compute` calls Hetzner Cloud API; VM boots, runs cloud-init.
7. Cloud-init: installs `cloudflared` + `oc` + noVNC, writes systemd units with env vars, drops firewall rules, starts services.
8. `cloudflared` dials Cloudflare; tunnel comes up.
9. `service-api` polls the tunnel via the BFF path; on healthy, marks `vms.is_active = true`.

No long-lived platform secret ever reaches the VM. The VM has only:
- Its own `OC_DASHBOARD_TOKEN` (only useful to the platform calling INTO it).
- Its own Cloudflare tunnel token (only useful for `cloudflared` to dial out).
- A user-scoped LLM-proxy JWT (only useful for the agent to call LLMs).

---

## 5. Data flow — VM destruction / token rotation

**Scenario:** user cancels; or operator rotates a suspected-compromised VM.

1. `service-api` receives cancel webhook (or operator action).
2. Marks `vms.is_active = false`; RLS now hides the row from user reads.
3. Calls `oc-compute` Worker: destroy VM. Hetzner reaps the machine; IP returns to pool.
4. Calls Cloudflare API: delete tunnel. The tunnel hostname is now gone.
5. Deletes the `vms` row after a grace period (for audit log linkage); KMS-encrypted `vm_token` is unrecoverable post-deletion.

Token rotation without destruction (Phase 6):

1. `service-api` generates a new token; KMS-encrypts.
2. Updates `vms.vm_token` atomically (the row, under transaction).
3. Pushes the new token to the VM via the BFF tunnel using an admin endpoint that authenticates with the OLD token.
4. VM systemd unit rotates the env var, restarts the backend.
5. Next BFF request uses the new token.

---

## 6. What runs where (responsibility matrix)

| Capability | Lives in | Why |
|---|---|---|
| User signup, sign-in | Supabase Auth + `oc-workspace` BFF | Standard auth pattern; SPA-friendly |
| Subscription billing | `service-api` (AWS) | Long-lived secrets (Razorpay) stay in one tightly-scoped server |
| VM provisioning / destruction | `service-api` → `oc-compute` Worker | Hetzner credential isolation |
| Per-VM tunnel routing | `oc-workspace` BFF | Hot-path proxy; session-derived |
| Chat agent loop | VM (`oc workspace backend`) | The agent IS the per-user computer |
| LLM calls | VM → our LLM proxy | Per-user JWT, central rate limit + observability |
| File I/O, browser, desktop, root tools | VM (`oc` agent) | Root by design |
| Capability token minting | `oc-workspace` BFF | Server-side authority; verified by VM in some flows |
| Tenant data store | Supabase Postgres + RLS | Single tenant boundary, enforced at storage layer |
| Audit log | Supabase Postgres (`audit_log` table) | Same RLS perimeter as the actions it records |

---

## 7. What is deliberately NOT in this architecture

- **No VM-to-VM direct communication.** VMs never talk to each other. Cross-user "collaboration" features are deferred until a fundamentally different model.
- **No customer-supplied integrations on shared infra.** Plugins / MCPs / hooks run inside the user's own VM where the blast radius is themselves; no shared marketplace runtime.
- **No platform-held LLM keys exposed to VMs.** The LLM proxy interposes; the VM has only its user-scoped JWT.
- **No persistent BFF-side caching of `vm_token`.** Each request decrypts via KMS at request time. (Performance hit accepted for blast-radius reduction; revisit if it becomes a real problem.)
- **No raw SQL from the BFF outside `server/admin/*`.** All user-context queries go through Drizzle with user-context credentials so RLS is always in force.

---

## 8. How this document gets updated

- Material architectural changes (new component, removed component, changed trust boundary) require:
  1. PR pair updating this document in both repos.
  2. Corresponding update to [`docs/THREAT-MODEL.md`](THREAT-MODEL.md) (new Actor or extended row).
  3. Corresponding update to [`docs/SECURITY-INVARIANTS.md`](SECURITY-INVARIANTS.md) (new invariant or enforced change).
  4. Note in `docs/plans/tryopencomputer-platform-build-2026-05-18.md` decision log.
- Cosmetic / wording changes do not require all four; just keep both mirrors in sync.

## 9. Cross-references

- Threats: [`docs/THREAT-MODEL.md`](THREAT-MODEL.md)
- Invariants: [`docs/SECURITY-INVARIANTS.md`](SECURITY-INVARIANTS.md)
- Plan: [`docs/plans/tryopencomputer-platform-build-2026-05-18.md`](plans/tryopencomputer-platform-build-2026-05-18.md) §5 (summary) and §8 (per-phase implementation).
