# OpenClaw browser — noVNC sandbox bridge (RDP-style remote view + control)

> Captured from a read-only deep-dive subagent (2026-05-16). Treat as a skeleton; JIT deeper read of the named files when porting. Continues the `01`–`06` series — see those for the browser core, CDP/session, Playwright tools, AI/snapshot, the HTTP control layer, and the agent-side client. This file documents the GAP: the noVNC bridge that lets a human watch and drive the agent's *sandboxed* browser, RDP-style.

## One-line summary

When the agent's browser runs inside a Docker sandbox (`agents.defaults.sandbox.browser.enabled`), OpenClaw layers a **noVNC stack inside the container** (`Xvfb` → `x11vnc` → `websockify` → bundled noVNC web client) and gates human access through a **two-token funnel**: the long-lived loopback bridge server (Bearer/password, see `05`) plus a **one-time, 60-second observer token** that the bridge trades for a credential-carrying bootstrap page — so the VNC password never lands in a URL bar's query string, a server log, or a `Location:` header.

## Where this lives in the repo (entirely under `src/agents/sandbox/` + the browser image)

This is **not** part of the `extensions/browser/` plugin's browser-core. It is host-side sandbox orchestration plus an in-container shell entrypoint:

| Concern | File |
|---|---|
| noVNC token mint / consume / TTL prune | `src/agents/sandbox/novnc-auth.ts` |
| Sandbox-browser container lifecycle (creates the noVNC pipeline) | `src/agents/sandbox/browser.ts` |
| In-container display + VNC + websockify launch | `scripts/sandbox-browser-entrypoint.sh` |
| Sandbox-browser Docker image (installs `novnc`/`x11vnc`/`xvfb`/`websockify`) | `scripts/docker/sandbox/Dockerfile.browser` |
| Bridge HTTP route that serves the noVNC bootstrap page | `extensions/browser/src/browser/bridge-server.ts` (lines 57–145) |
| Config shape (`vncPort`, `noVncPort`, `enableNoVnc`, `headless`) | `src/config/types.sandbox.ts:74–78`, `src/agents/sandbox/types.ts:32–47` |
| Defaults (`5900` / `6080`) | `src/agents/sandbox/constants.ts:46–49` |
| Config resolution / defaulting | `src/agents/sandbox/config.ts:145–155` |
| Config-hash inputs (recreate trigger) | `src/agents/sandbox/config-hash.ts:13–25` |
| Registry persistence (`noVncPort` recorded) | `src/agents/sandbox/registry.ts:34–41` |
| CLI display of the mapped noVNC port | `src/commands/sandbox-display.ts:56–82` |
| Security audit of the published port | `src/security/audit-sandbox-browser.test.ts` (and `audit-extra.async.js`) |
| Sandbox doc (user-facing) | `docs/gateway/sandboxing.md:20–30`, `docs/gateway/config-agents.md:882–883` |

The `02`/`05` notes already cover CDP and the bridge server's auth/route machinery — **this file does not re-derive them**, it only adds the noVNC surface that sits beside them.

## Why noVNC at all (vs. the screenshot path in `04`)

The browser core (`03`/`04`) already gives the *agent* eyes — `POST /screenshot`, `POST /snapshot`. noVNC is **not for the agent**. It is for the **human**, and it solves a problem screenshots cannot:

- The sandboxed Chromium runs inside a container with no display the host can see.
- Screenshots are a poll: discrete frames, agent-driven, no input path back.
- noVNC is a **live, interactive RDP-style channel** — the human sees every frame of what the agent's browser is doing in real time, and (because RFB is bidirectional) can take the mouse/keyboard to rescue a stuck login, solve a CAPTCHA, or correct the agent.

So the design intent: the agent drives via CDP; the human observes — and optionally co-drives — via noVNC. The `mantis.md` concept doc (`docs/concepts/mantis.md:305,322`) names this explicitly as the "observer browser" + "VNC or noVNC for rescue" pattern.

## The in-container pipeline (`scripts/sandbox-browser-entrypoint.sh`)

The browser image's entrypoint is one bash script that supervises a 4-or-5 process tree. The display/VNC half is conditional on `ENABLE_NOVNC=1 && HEADLESS!=1`:

```
Xvfb :1                       virtual X display, 1280x800x24, -nolisten tcp   (entrypoint.sh:92)
   │  DISPLAY=:1
   ▼
chromium --user-data-dir=...  rendered onto :1 (NOT --headless when noVNC on)  (entrypoint.sh:145)
   │
   ▼  (display :1 is scraped)
x11vnc -display :1            RFB server on VNC_PORT, -localhost, -rfbauth     (entrypoint.sh:199)
   -shared -forever -rfbauth ${HOME}/.vnc/passwd -localhost
   │  RFB/5900
   ▼
websockify --web /usr/share/novnc/ ${NOVNC_PORT} localhost:${VNC_PORT}        (entrypoint.sh:203)
   │  serves the bundled noVNC web client AND bridges WS↔TCP-RFB
   ▼
noVNC web client (vnc.html)   WebSocket on NOVNC_PORT (default 6080)
```

Key details, line-cited:

- **Headless is mutually exclusive with noVNC.** `entrypoint.sh:188` gates the whole VNC block on `HEADLESS != 1`; `isNoVncEnabled()` (`novnc-auth.ts:30`) mirrors this host-side: `enableNoVnc && !headless`. A headless Chromium has nothing to scrape, so noVNC is silently skipped. Chromium only gets `--headless=new` when `HEADLESS=1` (`entrypoint.sh:118`).
- **`Xvfb` is the capture surface.** There is no GPU compositor and no real display — `Xvfb` is an in-memory framebuffer at `:1`. Chromium renders into it; `x11vnc` scrapes it. This is screen-scraping, not a Chromium-native capture API.
- **`x11vnc` is bound `-localhost`** (`entrypoint.sh:199`) — the RFB port (`5900`) is reachable only from inside the container's loopback. The only thing that talks to it is `websockify` in the same container. The RFB port is **never** published to the host (only `9222` CDP and `6080` noVNC are — see container-edge section below).
- **The VNC password is written to a file** at `${HOME}/.vnc/passwd` via `x11vnc -storepasswd`, `chmod 600` (`entrypoint.sh:196–197`), then consumed via `-rfbauth`. It is **not** passed on a command line that would show in `ps`.
- **`websockify` does double duty** (`entrypoint.sh:203`): `--web /usr/share/novnc/` serves the static noVNC client (the `novnc` apt package, `Dockerfile.browser:20`), and the positional `${NOVNC_PORT} localhost:${VNC_PORT}` makes it a WebSocket-to-TCP relay onto the RFB port. One process, both halves of the bridge.
- **`wait -n`** (`entrypoint.sh:209`) — the container stays up as long as any supervised process lives; a `cleanup()` trap (`entrypoint.sh:49–88`) `TERM`s then `KILL`s the whole tree (`WEBSOCKIFY_PID`, `X11VNC_PID`, `SOCAT_PID`, `CHROME_PID`, `XVFB_PID`) on exit/INT/TERM.

`socat` (`entrypoint.sh:177–186`) is the **CDP** relay (a separate concern — see `02`); it is in this same script but unrelated to noVNC. It only starts if `OPENCLAW_BROWSER_CDP_SOURCE_RANGE` is set, and binds with a `range=` CIDR allowlist.

## The container image (`scripts/docker/sandbox/Dockerfile.browser`)

A 35-line Debian-bookworm-slim image. The noVNC-relevant apt packages (`Dockerfile.browser:9–25`):

```
chromium    novnc    websockify    x11vnc    xvfb    socat    curl    python3
```

`EXPOSE 9222 5900 6080` (`Dockerfile.browser:33`) — CDP, raw VNC, noVNC. (`EXPOSE` is documentation only; the actual publish decision is made host-side in `browser.ts` — see next section.) Runs as a non-root `sandbox` user (`Dockerfile.browser:29–31`). `CMD ["openclaw-sandbox-browser"]` is the entrypoint script copied to `/usr/local/bin`.

## Host-side container creation (`src/agents/sandbox/browser.ts`)

`ensureSandboxBrowser()` is the host-side orchestrator. The noVNC-relevant decisions, line-cited:

1. **Enabled gate.** `noVncEnabled = isNoVncEnabled(params.cfg.browser)` (`browser.ts:211`) → `enableNoVnc && !headless`.
2. **Password generation, host-side, per container** (`browser.ts:254–255`): on a *fresh* container, `noVncPassword = generateNoVncPassword()` — an 8-char alphanumeric string from `crypto.randomInt` (`novnc-auth.ts:34–41`). 8 chars because VNC's RFB DES auth caps the password at 8. (The entrypoint has its own fallback at `entrypoint.sh:189–193` — derive 8 chars off `/proc/sys/kernel/random/uuid` — but in the normal path the host always supplies one.)
3. **Password re-read on an *existing* container** (`browser.ts:214–218`, `349–352`): rather than mint a new password for a container that's already running, the host reads `OPENCLAW_BROWSER_NOVNC_PASSWORD` back out of the live container env via `docker inspect` (`readDockerContainerEnvVar`). So the password is **stable for the container's lifetime** and survives a host-process restart.
4. **Port publishing — loopback-pinned** (`browser.ts:314–317`):
   ```
   args.push("-p", `127.0.0.1::${cdpPort}`);            // always
   if (noVncEnabled) args.push("-p", `127.0.0.1::${noVncPort}`);   // only when noVNC on
   ```
   The `127.0.0.1::<port>` form means **bind to host loopback, ephemeral host port**. The container's `6080` is reachable only from the host's `127.0.0.1` on some Docker-assigned high port — never `0.0.0.0`. The raw VNC port `5900` is deliberately **not** in this list; it never leaves the container.
5. **Env injected into the container** (`browser.ts:318–333`): `OPENCLAW_BROWSER_HEADLESS`, `OPENCLAW_BROWSER_ENABLE_NOVNC`, `OPENCLAW_BROWSER_VNC_PORT`, `OPENCLAW_BROWSER_NOVNC_PORT`, and — only when `noVncEnabled && noVncPassword` — `OPENCLAW_BROWSER_NOVNC_PASSWORD=<password>` (`browser.ts:331–333`). The env key is the constant `NOVNC_PASSWORD_ENV_KEY` (`novnc-auth.ts:4`).
6. **Resolve the mapped host port** (`browser.ts:346–348`): `mappedNoVnc = readDockerPort(containerName, noVncPort)` — `docker port <name> 6080/tcp` parsed for the `:NNNNN` tail (`docker.ts:259–273`).
7. **Mint the observer URL** (`browser.ts:452–461`) — see next section.

The returned `SandboxBrowserContext` (`types.ts:83–87`) is `{ bridgeUrl, noVncUrl?, containerName }`.

## Bridge authentication — the two-token funnel

This is the security core and the part `05` does not cover. Watching the agent's sandboxed browser requires clearing **two** independent gates.

### Gate 1 — the bridge server (covered by `05`, recapped)

The per-sandbox **bridge server** (`extensions/browser/src/browser/bridge-server.ts`) is the same loopback Express app `05` describes: `127.0.0.1`-only, Bearer-or-password auth (`installBrowserAuthMiddleware`), CSRF-on-loopback. Its auth is held in the in-process `bridge-auth-registry.ts` keyed by port (`05` §"two servers"). Sandbox creation wires that auth from the **same browser-control credentials** loopback browser clients already use — `ensureBrowserControlAuth()` is resolved in `context.ts:184–200` and threaded in as `bridgeAuth` → `desiredAuthToken/desiredAuthPassword` (`browser.ts:359–367`, default `crypto.randomBytes(24).toString("hex")` = 48 hex chars if nothing else exists).

### Gate 2 — the one-time noVNC observer token (`src/agents/sandbox/novnc-auth.ts`)

The bridge server's Bearer token is **long-lived**. Handing that to a human to paste into a browser, or embedding it in a shareable URL, would leak a credential that controls the *entire* browser API (`/profiles`, `/tabs`, CDP URLs — every route in `05`'s route map). So noVNC access uses a **second, ephemeral, single-use** token, scoped to *only* the VNC observer.

`novnc-auth.ts` is a self-contained token broker — an in-process `Map<token, {noVncPort, password?, expiresAt}>` (`NO_VNC_OBSERVER_TOKENS`, line 20):

- **`issueNoVncObserverToken({noVncPort, password, ttlMs})`** (`novnc-auth.ts:58–73`) — mints `crypto.randomBytes(24).toString("hex")` (48 hex chars), stores `{noVncPort, password, expiresAt: now + ttlMs}`. Default TTL `NOVNC_TOKEN_TTL_MS = 60_000` — **60 seconds** (line 5). Prunes expired entries on every issue.
- **`consumeNoVncObserverToken(token)`** (`novnc-auth.ts:75–94`) — looks the token up, **`delete`s it immediately** (line 89 — single-use, even on a same-millisecond second call), then checks expiry. Returns `{noVncPort, password}` or `null`.
- **`resetNoVncObserverTokensForTests()`** (line 101) — test-only clear.

So the observer token is: random 48-hex, **60-second** lifetime, **one-time** (consumed on first use). It carries the VNC password as its *payload* — the password travels server-to-server inside the token map, not on any wire the human sees until the very last hop.

### How the two gates compose at the bridge route

`bridge-server.ts:83–105` registers `GET /sandbox/novnc` **only if** `params.resolveSandboxNoVncToken` was supplied (it is — `browser.ts:427` passes `consumeNoVncObserverToken`). The handler:

1. `if (!hasVerifiedBrowserAuth(req))` → **401** (line 85). **Gate 1**: the request must already carry the bridge's Bearer/password — `installBrowserAuthMiddleware` ran first, `hasVerifiedBrowserAuth` reads the flag it set.
2. Sets hard anti-leak headers (lines 88–92): `Cache-Control: no-store, no-cache, must-revalidate, proxy-revalidate`, `Pragma: no-cache`, `Expires: 0`, `Referrer-Policy: no-referrer`.
3. `rawToken = req.query.token` → **400** if missing (lines 93–97).
4. `resolved = resolveSandboxNoVncToken(rawToken)` → **404** "Invalid or expired token" if `null` (lines 98–102). **Gate 2**: `consumeNoVncObserverToken` validates + burns the one-time token.
5. On success, returns a **bootstrap HTML page** (`buildNoVncBootstrapHtml`, lines 28–55) — *not* a redirect.

### Why a bootstrap page, not a 302 redirect

`buildNoVncBootstrapHtml` (`bridge-server.ts:28–55`) emits a tiny HTML doc whose only logic is:

```js
const target = "http://127.0.0.1:<noVncPort>/vnc.html#autoconnect=1&resize=remote&password=<pw>";
window.location.replace(target);
```

The password rides in the **URL fragment** (`#…`), and the navigation is a **client-side `window.location.replace`**. This is deliberate — the CHANGELOG records it as a hardening fix (`CHANGELOG.md:5971`: *"replace noVNC token redirect with a bootstrap page that keeps credentials out of `Location` query strings"*):

- A `302` would put the password in a `Location:` response header → reverse-proxy logs, browser history of the *redirect*.
- A query-string password (`?password=`) → server access logs, `Referer` headers.
- A **fragment** (`#password=`) is never sent to any server, never logged server-side; `window.location.replace` also leaves no back-button history entry. `Referrer-Policy: no-referrer` + `<meta name="referrer" content="no-referrer">` (line 44) stop the fragment-bearing URL from leaking via `Referer` on the next hop into noVNC.
- noVNC's `vnc.html` reads `autoconnect`, `resize`, and `password` straight out of its own fragment — so the bootstrap hands the credential to the noVNC client and nothing else.

The history of this hardening (CHANGELOG, newest-relevant first):

- `CHANGELOG.md:5971` — *increase observer password entropy, shorten observer token lifetime, replace the token redirect with a no-cache/no-referrer bootstrap page.*
- `CHANGELOG.md:6849` — *require VNC password auth for noVNC observer sessions, plumb per-container passwords, emit short-lived observer token URLs, keep loopback-only host publishing.*
- `CHANGELOG.md:7418` — *require auth for the sandbox browser bridge server at all.*
- `CHANGELOG.md:3213` — noVNC swept into the broader browser/sandbox SSRF hardening pass.

### The observer URL the host hands out

After mapping the host port, `browser.ts:452–461` mints the final URL:

```ts
const token = issueNoVncObserverToken({ noVncPort: mappedNoVnc, password: noVncPassword });
return buildNoVncObserverTokenUrl(resolvedBridge.baseUrl, token);
// → http://127.0.0.1:<bridgePort>/sandbox/novnc?token=<48-hex>
```

`buildNoVncObserverTokenUrl` (`novnc-auth.ts:96–99`) just appends `?token=`. Note what this URL **does** and **does not** carry:

- It carries the **one-time, 60-second** observer token — useless after one fetch or 60s.
- It does **not** carry the VNC password (verified by test `browser.create.test.ts:220`: `expect(result?.noVncUrl).not.toContain("password=")`).
- It points at the **bridge server** (Gate 1 still applies — you also need the bridge Bearer to even reach `/sandbox/novnc`).

So the URL alone is safe-ish to log; it is two-factor (needs the bridge credential too) and self-expiring.

`novnc-auth.ts` also exposes `buildNoVncDirectUrl(port)` (line 43, `http://127.0.0.1:<port>/vnc.html`) and `buildNoVncObserverTargetUrl({port, password})` (lines 47–56, the fragment-bearing direct URL) — these are the *inner* target the bootstrap page redirects to; helpers/tests use them, the user-facing path uses the token URL.

## The RDP-style interaction flow — view *and* control

Once the human's browser has loaded `vnc.html` and the noVNC client has WebSocket-connected through `websockify` to `x11vnc`, the channel is **standard RFB** — and RFB is inherently bidirectional:

- **Frames out (view):** `x11vnc` scrapes the `Xvfb :1` framebuffer and pushes RFB `FramebufferUpdate` messages; noVNC paints them onto a `<canvas>`. `#resize=remote` (in the bootstrap fragment) asks the server to match the client viewport.
- **Input in (control):** every mouse move/click/scroll and keystroke in the noVNC canvas becomes an RFB `PointerEvent`/`KeyEvent`, travels back over the same WebSocket, through `websockify` to `x11vnc`, which **synthesizes the input onto the X display `:1`** — which Chromium is rendering into. The human is literally moving the same cursor the agent's Chromium sees.

This is why it is "RDP-style": one bidirectional pipe, live framebuffer one way, input events the other — like RDP/VNC of a remote desktop, except the "desktop" is a single sandboxed Chromium on a headless `Xvfb`.

### View-only vs. control — what gates it

There is **no application-layer view-only mode** in this checkout. The view/control split is governed entirely by lower layers:

- **`x11vnc -shared`** (`entrypoint.sh:199`) — multiple noVNC clients may attach simultaneously; all of them are full read-write RFB sessions. There is no `-viewonly` flag passed, and the host code never offers one.
- **The agent (CDP) and the human (noVNC) drive the *same* Chromium concurrently.** CDP input and synthesized X input both land on the one browser. There is no arbitration — if both move the mouse at once, last-writer-wins at the OS input layer. In practice the human "takes over" by simply acting while the agent is idle (the rescue pattern).
- The only access control is the **two-token funnel** above — clear both gates and you get a full read-write RFB session. "View-only" would have to be added (an `x11vnc -viewonly` second listener, or an RFB-aware filtering proxy) — see the OC-port section.

`allowHostControl` (`SandboxBrowserConfig.allowHostControl`, `types.ts:43`; `sandboxing.md:26`) is a **different** axis — it governs whether a *sandboxed agent session* may target the *host* browser instead of its sandbox one. It is unrelated to noVNC human control.

## Lifecycle — bridge tied to the sandbox-browser session

The noVNC surface has no independent lifecycle; it is strictly a child of the sandbox-browser container + its bridge server.

**Start** (all inside `ensureSandboxBrowser`, `browser.ts:162–468`):
1. Container created with `-p 127.0.0.1::6080` + the noVNC env (only if `noVncEnabled`).
2. `docker start` → the entrypoint brings up `Xvfb`/`x11vnc`/`websockify` (only if `ENABLE_NOVNC=1 && HEADLESS!=1`).
3. Host resolves `mappedNoVnc` via `docker port`.
4. The bridge server starts (`startBrowserBridgeServer`, `browser.ts:416–428`), and **because** `resolveSandboxNoVncToken` is passed, it registers `GET /sandbox/novnc`.
5. Host mints the observer token + URL and returns it on `SandboxBrowserContext.noVncUrl`.

**Bridge reuse vs. teardown** (`browser.ts:354–390`): an existing bridge for the same `scopeKey` is reused only if `containerName` matches, the CDP port matches, the SSRF policy matches, **and** the auth matches (`shouldReuse && policyMatches && authMatches`). Any mismatch → `stopBrowserBridgeServer(existing)` + drop from the `BROWSER_BRIDGES` map, then a fresh bridge. A new bridge means the old `/sandbox/novnc` route (and its in-process token map state) is gone.

**Recreate on config drift** (`browser.ts:188–250`): `computeSandboxBrowserConfigHash` folds `vncPort`, `noVncPort`, `enableNoVnc`, `headless` (among others — `config-hash.ts:13–25`) into a hash stored as the `openclaw.configHash` Docker label. On the next `ensureSandboxBrowser`, a hash mismatch means the container is **removed and recreated** (unless it was used in the last 5 min — `HOT_BROWSER_WINDOW_MS`, then it only logs an `openclaw sandbox recreate --browser` hint). So flipping `enableNoVnc` or `headless` in config eventually rebuilds the whole pipeline.

**Stop** — three independent layers:
- The **container** stops/`rm`s via the normal sandbox lifecycle (`openclaw sandbox recreate`, prune-on-idle); the entrypoint's `cleanup()` trap kills `websockify`/`x11vnc`/`Xvfb` together.
- The **bridge server** stops via `stopBrowserBridgeServer` (`bridge-server.ts:147–159`), which also `deleteBridgeAuthForPort`s.
- The **observer token** self-destructs — 60s TTL, or consumed on first `/sandbox/novnc` hit. No explicit revoke needed; `pruneExpiredNoVncObserverTokens` runs on every issue/consume.

**Registry** (`registry.ts:34–41`, `browser.ts:441–450`): `updateBrowserRegistry` records `noVncPort` (the *container* port, not the mapped host port) in `SANDBOX_BROWSER_REGISTRY_PATH` (`browsers.json`). `openclaw sandbox list` / `displayBrowsers` (`sandbox-display.ts:56–82`) prints `noVNC: <port>` for each browser container.

## ASCII diagram — the streaming + control path

```
  ┌─────────────── HOST ────────────────────────────────────────────────────┐
  │                                                                          │
  │  human's browser                                                         │
  │  ┌──────────────┐   1. GET /sandbox/novnc?token=<48hex>                   │
  │  │ noVNC client │      Authorization: Bearer <bridge-token>   ◀── GATE 1  │
  │  │  (vnc.html)  │ ───────────────────────────┐                            │
  │  └──────┬───────┘                            ▼                            │
  │         │                       ┌────────────────────────────┐           │
  │         │  2. bootstrap HTML     │  Bridge server (Express)   │           │
  │         │     window.location    │  127.0.0.1:<ephemeral>     │           │
  │         │     .replace(          │  /sandbox/novnc handler:   │           │
  │         │      vnc.html#         │   • hasVerifiedBrowserAuth │── GATE 1  │
  │         │      autoconnect=1&    │   • consumeNoVncObserver-  │── GATE 2  │
  │         │      password=<pw>)    │     Token()  (1-time, 60s) │   (burns  │
  │         │ ◀──────────────────────│   • no-cache/no-referrer   │    token, │
  │         │                        │   • returns bootstrap HTML │    yields │
  │         │                        └────────────────────────────┘    pw)   │
  │         │  3. WebSocket (RFB-over-WS), password in #fragment only         │
  │         ▼                                                                 │
  │  127.0.0.1:<mapped 6080>  ──── docker -p 127.0.0.1::6080 ────┐            │
  │                                                              │            │
  │  ┌──────────── SANDBOX CONTAINER (Docker) ───────────────────▼────────┐  │
  │  │                                                                     │  │
  │  │   websockify  :6080   --web /usr/share/novnc/                       │  │
  │  │     │  serves noVNC client  +  WS◄──►TCP relay                       │  │
  │  │     │                                                               │  │
  │  │     ▼  RFB/TCP   localhost:5900   (never published to host)          │  │
  │  │   x11vnc  :5900   -localhost -shared -rfbauth ~/.vnc/passwd          │  │
  │  │     ▲ scrape framebuffer            │ synthesize PointerEvent/       │  │
  │  │     │ (frames OUT ──▶ human)        ▼ KeyEvent  (control IN ◀── human)│  │
  │  │   Xvfb  :1   1280x800x24   (in-memory framebuffer, no GPU)           │  │
  │  │     ▲ render                                                         │  │
  │  │     │                                                                │  │
  │  │   chromium --user-data-dir=...   ◀── CDP/9222 ◀── socat ◀── agent    │  │
  │  │            (the agent drives here; the human co-drives via RFB)      │  │
  │  └─────────────────────────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────────────┘

  Frames :  Xvfb framebuffer ──▶ x11vnc ──▶ websockify ──▶ WS ──▶ noVNC <canvas>
  Control:  noVNC input ──▶ WS ──▶ websockify ──▶ x11vnc ──▶ synth X input ──▶ Xvfb :1
  Agent  :  CDP ──▶ socat ──▶ Chromium  (same browser the human sees; no arbitration)
```

## Honest gap — `noVncUrl` is computed but not threaded to the user in this checkout

`ensureSandboxBrowser` returns `noVncUrl` on `SandboxBrowserContext` (`browser.ts:465`, `types.ts:85`), and `docs/gateway/config-agents.md:882` claims *"noVNC URL injected into system prompt."* **In this checkout that claim is not borne out by the code.** Tracing every reader of `noVncUrl` across the whole tree (`grep -rn noVncUrl --include=*.ts`):

- `buildEmbeddedSandboxInfo` (`pi-embedded-runner/sandbox-info.ts:39–59`) builds `EmbeddedSandboxInfo` from the sandbox context but copies **only `browser?.bridgeUrl`** → `browserBridgeUrl`. `EmbeddedSandboxInfo` (`pi-embedded-runner/types.ts:215–229`) has **no `noVncUrl` field**.
- The system prompt (`system-prompt.ts:953`) emits only the boolean `"Sandbox browser: enabled."` from `browserBridgeUrl` — it never renders a noVNC URL.
- Every other `noVncUrl` hit is a **test** (`browser.create.test.ts:219–220,236`, `pi-embedded-runner.buildembeddedsandboxinfo.test.ts:36`) or the producer/type itself.

So: the **bridge + auth + RFB pipeline is fully built and the URL is minted**, but the last hop — surfacing that URL to the human (system prompt, gateway message, or CLI) — is **absent or stripped** in this checkout. The CLI (`sandbox-display.ts`) shows the noVNC *port number* but not the tokenized observer URL. Treat the config-agents.md sentence as aspirational/stale relative to this code. A port should decide deliberately how the human learns the URL.

## What an OC port would need

OpenComputer's browser story today is `extensions/browser-harness/` (a Hermes-derived `BrowserHarness`), and OC has no Docker sandbox-browser layer at all. A faithful port is a **net-new subsystem**, not a tweak. Concretely:

1. **A sandbox-browser container image + entrypoint.** Port `Dockerfile.browser` and `sandbox-browser-entrypoint.sh` ~verbatim — `xvfb` + `x11vnc` + `websockify` + the `novnc` web client, supervised by one script with a kill-the-tree cleanup trap. The bash entrypoint translates straight across; the env-var contract (`OPENCLAW_BROWSER_*`) becomes `OC_BROWSER_*`. Keep the headless⊕noVNC mutual exclusion.

2. **A host-side container orchestrator.** Port `ensureSandboxBrowser` — Docker `create`/`start`/`port`/`inspect`, config-hash recreate-on-drift, the registry JSON. Python: `subprocess`/`docker` SDK; the config-hash is a stable `hashlib` digest over the same field set. OC already has a per-profile state dir — `browsers.json` slots into `~/.opencomputer/<profile>/sandbox/`.

3. **The two-token funnel — this is the load-bearing security design, port it exactly.**
   - **Gate 1**: reuse OC's existing browser/gateway loopback auth (Bearer/password) for the bridge server. OC's gateway already has token auth — extend it to an ephemeral per-sandbox bridge keyed by port (mirror `bridge-auth-registry.ts`).
   - **Gate 2**: a Python equivalent of `novnc-auth.ts` — an in-process `dict[str, ObserverToken]`, `secrets.token_hex(24)` tokens, **60s TTL**, **single-use** (pop-on-consume), prune-on-touch. This is ~40 lines and trivially testable; do not skip it and do not hand out the bridge Bearer for VNC.
   - **The bootstrap-page trick is mandatory, not optional.** Serve an HTML page that `window.location.replace`s into `vnc.html#…password=…`. Never a 302; never `?password=`. Set `Cache-Control: no-store`, `Pragma: no-cache`, `Expires: 0`, `Referrer-Policy: no-referrer`, `<meta name=referrer content=no-referrer>`. This keeps the VNC password out of every server log and the `Location` header. (See `CHANGELOG.md:5971` for why each of these exists.)

4. **Loopback-only publishing, always.** Bind the container's noVNC port to `127.0.0.1::<port>` (ephemeral host port), never `0.0.0.0`. Never publish the raw RFB `5900`. Carry over OpenClaw's security audit (`audit-sandbox-browser.test.ts`) that flags any non-loopback published port.

5. **Per-container VNC password, host-minted, env-injected, re-read on reuse.** 8-char alphanumeric (RFB's 8-char cap). Mint fresh on container create; on an existing container, read it back from the live container env rather than rotating. `x11vnc -storepasswd` to a `chmod 600` file inside the container — never on a `ps`-visible command line.

6. **Decide and implement the human-facing last hop** — the gap §above. Pick one: inject the tokenized observer URL into the agent's system prompt (so the agent can offer it), emit it as a gateway/channel message ("watch live: <url>"), or print it from an `oc sandbox` CLI command. OpenClaw mints the URL but doesn't deliver it — OC should close that loop deliberately rather than copying the dead end.

7. **Optional but worth it: a real view-only mode.** OpenClaw has none — every noVNC client is full read-write. If OC wants a "show, don't touch" tier (e.g. for an audience), add a second `x11vnc -viewonly` listener on its own port + its own observer-token kind, and let the URL minter choose which. Cheap, and a genuine improvement over the source.

8. **Lifecycle binding.** The bridge + token + RFB pipeline must be strict children of the sandbox-browser container. Container teardown (recreate, idle-prune) kills the pipeline; bridge mismatch (port/auth/policy) tears down and rebuilds; observer tokens self-expire. No independent noVNC lifecycle — exactly as `browser.ts` does it.

### Cross-cutting porting notes

- The agent (CDP) and human (noVNC) drive the **same** Chromium with **no arbitration** — OpenClaw accepts last-writer-wins at the OS input layer. That is fine for the rescue use case; document it, don't try to "fix" it with a lock unless OC has a reason to.
- `headless ⊕ noVNC` is a hard invariant in two places (`isNoVncEnabled` host-side, the `HEADLESS!=1` gate in the entrypoint). Port both checks — a headless Chromium gives `x11vnc` nothing to scrape.
- Keep `socat` (CDP relay) and `websockify` (noVNC bridge) as the two separate relays they are — same entrypoint, unrelated jobs, separate enable gates (`CDP_SOURCE_RANGE` set vs. `ENABLE_NOVNC`).
- The 60-second observer-token TTL is short on purpose — the human is expected to click the freshly-minted URL right away. Do not lengthen it casually; if a port needs a longer-lived watch session, the right answer is the token bootstraps a *session*, not that the *token* lives longer.
