# OC parity with Hermes + OpenClaw — Part 3: Deferred Items

Date: 2026-05-16
Owner: Saksham
Companion files: `PART-1-brainstorm-and-audit.md`, `PART-2-plan-and-plan-audit.md` — read those first.

This file is the honest register of everything the v1 parity plan (PART-2) deliberately left out of scope: what each item is, why it was cut, where the reference material lives, and what would unblock it. It is the deliverable of **Milestone 5** (documentation-only).

Milestone 5 also produced three architecture-extraction docs under `docs/refs/` so the three largest deferred items can be implemented later **without re-reading the OpenClaw / Hermes source trees**. They are linked inline below.

---

## Summary

| # | Deferred item | Status | Reference extraction | Unblocks when |
|---|---|---|---|---|
| 1 | Multi-node fleet routing | Deferred to v2 | `docs/refs/openclaw/fleet-routing.md` | a concrete multi-device need |
| 2 | Full-duplex voice-call over chat platforms | Deferred to v2 | `docs/refs/hermes-agent/voice-mode.md` | demand for live channel voice |
| 3 | Sandbox network-egress rules per scope | Deferred to a sandbox refinement | — (OC-internal feature) | M1 sandbox scope is in production |
| 4 | Sandboxed browser + noVNC bridge | Deferred to v2 | `docs/refs/openclaw/browser/07-novnc-sandbox-bridge.md` | OC grows a Docker sandbox-browser layer |
| 5 | Microsoft Graph — Teams + SharePoint | Deferred to Graph v2 | — (extends M3) | M3 has shipped, on demand |
| 6 | NeuTTS voice cloning + custom voices | Deferred to NeuTTS v2 | — (extends M4) | M4 has shipped, on demand |
| 7 | Crabbox remote-testbox plugin | **Removed** from v1 | — | a recurring remote-VM CI workflow |
| 8 | Modal / Vercel Sandbox / Daytona backends | Deferred, cheap post-v1 | — (M2 resolver handles N backends) | demand for a specific provider |

---

## 1 — Multi-node fleet routing

**What it is.** "My phone talks to my laptop talks to my server through one gateway." A Tailscale mesh for L3 reachability, Bonjour/mDNS for same-LAN gateway discovery, wide-area DNS for cross-network discovery, a per-node `node-host` process, and a `nodes-screen` UI/CLI surface. OC's `opencomputer/gateway/` is single-machine and has none of this.

**Why deferred.** PART-1's winning approach (H) named this explicitly: *the best from OpenClaw on fleet routing is multi-week unknown territory.* Shipping a partial fleet implementation in a v1 timeline would either ship broken or force M1–M4 to slip.

**Reference.** `docs/refs/openclaw/fleet-routing.md` (M5/T5.1) — documents all five subsystems with `file:line` cites, the **star-topology** model (every node connects *into* one Gateway; nodes never peer; the mesh is 100% Tailscale's job), the node-join / discovery / request-routing flows, the two-pairing-store trust model, and a dependency-ordered port slice. Its §8 enumerates ~10 net-new OC components and notes that the first four (in-memory `NodeRegistry`, `role=node` connect frame, `node.invoke` handler + command policy, `node-host` process + `oc node` CLI) already deliver a working single-tailnet fleet — discovery and Bonjour are UX sugar layered on top.

**Unblocks when.** There is a concrete multi-device requirement. Begin with the doc's four-component slice; Tailscale (a CLI shell-out — OC bundles no WireGuard) is the only hard external dependency.

## 2 — Full-duplex voice-call over chat platforms

**What it is.** A live audio loop letting the user voice-converse with the agent inside a chat platform's voice channel (Discord VC), rather than only discrete TTS/STT tool calls.

**Why deferred.** PART-1 named it a multi-week unknown alongside fleet routing.

**Reference.** `docs/refs/hermes-agent/voice-mode.md` (M5/T5.2). A key finding **narrows the gap**: OC is already near-parity on the *CLI* voice surface — `extensions/voice-mode/` and `opencomputer/voice/` already provide audio capture, VAD, STT, TTS, and a continuous loop. The genuine deferred gap is the **platform-bridged** live audio loop — chiefly the Discord `VoiceReceiver` (hand-rolled RTP parse + NaCl transport decrypt + DAVE E2EE decrypt + per-SSRC Opus decode, ~350 LOC) plus a gateway-side bridge.

**Size.** ~600–800 LOC + three new dependencies (`discord.py[voice]` / `PyNaCl` / `libopus`, `davey`, `ffmpeg`). The doc's §14 maps each Hermes piece onto an OC target. Matrix/Telegram voice *messages* (discrete files, not a live stream) are much smaller and could land independently.

**Unblocks when.** There is demand for live voice in Discord/Matrix channels.

## 3 — Sandbox network-egress rules per scope

**What it is.** Per-scope network-egress restriction for the sandbox — e.g. block egress to RFC1918 ranges, or a no-network scope.

**Why deferred.** PART-1's YAGNI sweep (§9) explicitly cut this from M1: *"Default = no network restriction; 'block egress to RFC1918' is a v2 feature."* M1 ships the scope policy (`none` / `agent` / `session` / `tool`); egress control is an orthogonal refinement layered on afterward.

**Reference.** None — this is an OC-internal feature, not a port from a reference repo. The implementation extends M1's `SandboxPolicy` with an egress-rules field and threads it into each sandbox backend's container/process creation.

**Unblocks when.** M1's sandbox scope has shipped and there is a concrete need for network isolation (e.g. running untrusted code).

## 4 — Sandboxed browser + noVNC bridge

**What it is.** The agent drives a browser inside a Docker sandbox; a human watches it live and can take mouse/keyboard control, RDP-style, through a noVNC (VNC-over-WebSocket) bridge.

**Why deferred.** It is a net-new subsystem — OC's browser story today is `extensions/browser-harness/` with no Docker sandbox-browser layer at all.

**Reference.** `docs/refs/openclaw/browser/07-novnc-sandbox-bridge.md` (M5/T5.3), which completes the existing `docs/refs/openclaw/browser/01`–`06` series. It documents the in-container pipeline (`Xvfb` → `chromium` → `x11vnc` → `websockify` → bundled noVNC client), the **two-token funnel** security design (a long-lived bridge credential plus a one-time, 60-second observer token), the bootstrap-page credential-hardening trick (keeps the VNC password out of `Location` headers and server logs), and the RDP-style bidirectional view+control flow. Honest note carried from the extraction: OpenClaw mints the observer URL but never delivers it to a user in the studied checkout — an OC port must close that last hop deliberately.

**Unblocks when.** OC grows a Docker sandbox layer (M2's E2B work is ephemeral *exec*, a different shape) and there is demand for "watch / rescue the agent's browser" workflows.

## 5 — Microsoft Graph: Teams + SharePoint

**What it is.** Extending the M3 Microsoft Graph tool beyond mail + calendar + OneDrive to cover Teams and SharePoint.

**Why deferred.** PART-1 §6 and the audit conclusion scoped M3 v1 to "send mail + read calendar + list OneDrive files." Teams/SharePoint bring their own payload shapes, paging, and throttling — a separate body of work. Graph v2.

**Reference.** None in M5. M3's own task T3.1 produces a Graph extraction doc (`docs/refs/hermes/microsoft-graph.md`) when M3 runs; Teams/SharePoint would extend that.

**Unblocks when.** M3 has shipped and there is demand for Teams/SharePoint.

## 6 — NeuTTS voice cloning + custom voices

**What it is.** Voice cloning and custom-voice synthesis, beyond default-voice local TTS.

**Why deferred.** PART-1 §9: *"Our v1 only needs default-voice TTS."* M4 ships NeuTTS as an optional local-voice tool with the default voice; cloning is NeuTTS v2.

**Reference.** None — extends M4.

**Unblocks when.** M4 has shipped and there is demand for custom/cloned voices.

## 7 — Crabbox remote-testbox plugin — REMOVED, not merely deferred

**What it was.** A plugin wrapping OpenClaw's Crabbox CLI to lease a remote Linux VM, sync a dirty checkout, run remotely, and release.

**Status.** **Removed from the plan entirely.** PART-2's revised Milestone 2 replaced the original "Crabbox plugin" with the **E2B ephemeral-sandbox backend**. The rationale: Crabbox is the wrong shape for OC's agent use case — it leases full VMs for minutes-to-hours (optimised for "run my CI suite on a remote beefy box"), whereas an agent making risky tool calls needs **per-call, fast-boot, ephemeral** containers, which is E2B's job. E2B also integrates with OC's existing `opencomputer/sandbox/` shape with no Go-CLI shell-out and no broker-config dance.

**Reference.** None.

**Re-evaluation trigger** (verbatim from PART-2): *"when there is a real, recurring 'run OC's pytest suite on a remote VM weekly' workflow."* Until then, E2B plus the user's own hosting box (Hetzner / Fly / Render) covers the agent-sandbox use case completely.

## 8 — Modal / Vercel Sandbox / Daytona sandbox backends

**What it is.** Additional sandbox execution backends beyond E2B (which M2 ships). Hermes ships all three.

**Why deferred.** M2 ships E2B plus `opencomputer/sandbox/resolver.py`, and the resolver is designed to handle N backends. Adding more is then cheap — each is a new backend file (`opencomputer/sandbox/{modal,vercel,daytona}.py`) implementing the same `SandboxBackend` interface as the existing `docker.py` / `linux.py` / `macos.py` / `ssh.py`.

**Reference.** None — the M2 resolver plus the existing backend files are the template.

**Unblocks when.** M2's E2B backend + resolver have landed and there is demand for a specific provider. Low-effort per backend.

---

## How to pick up a deferred item

1. **Read the linked reference extraction** (items 1, 2, 4) — they are written to be implementable without re-reading the source repos.
2. Run the **PART-1 → PART-2 workflow** (brainstorm → design-audit → plan → plan-audit) scoped to that single item.
3. Items **5, 6, 8** are version-bump extensions of a shipped milestone (M3, M4, M2 respectively) — they do not need a fresh brainstorm, just a scoped plan once the parent milestone has shipped.
4. Item **3** is an OC-internal refinement of M1 — it extends `SandboxPolicy`; plan it once M1 is in production.
5. Item **7** (Crabbox) is *removed*, not deferred — only revisit it against the specific trigger above.

---

## Honest closing note

Every item here was cut on the explicit principle, stated in PART-1 and PART-2, of **not shipping partial implementations of multi-week-unknown work in a v1 timeline**. The three reference extractions (items 1, 2, 4) are the down-payment that makes the v2 versions cheap to start: a future implementer inherits a `file:line`-cited architecture doc instead of a cold reference-repo read.
