# OpenClaw Fleet Routing — Architecture Extraction

Reference: OpenClaw monorepo at `sources/openclaw/` (TypeScript/JS, pnpm). All
`path:line` citations below are relative to that checkout. This doc is a
self-contained spec for porting OpenClaw's multi-node fleet routing onto OC's
`opencomputer/gateway/`.

---

## 1. Scope and the "fleet" mental model

OpenClaw's fleet is a **star, not a mesh** at the application layer. There is
exactly one **Gateway** process; every other device is a **node** (a peripheral
that connects *into* the Gateway). "My phone talks to my laptop talks to my
server" works because:

- All three devices join one **Tailscale tailnet** (the actual mesh — WireGuard).
- One device runs the **Gateway**; the other two run **node hosts**.
- The agent always runs on the Gateway. A node never talks to another node
  directly — it sends a request to the Gateway, and the Gateway forwards a
  `node.invoke` to the target node. The Gateway is the router.

So "fleet routing" decomposes into five subsystems, all found in this checkout:

| Subsystem | Job | Found in |
|---|---|---|
| **Tailscale** | Cross-network L3 reachability (mesh VPN). OpenClaw does not implement mesh; it shells out to the `tailscale` CLI. | `src/infra/tailscale.ts`, `src/gateway/server-tailscale.ts` |
| **Bonjour / mDNS** | Same-LAN auto-discovery of the Gateway. A bundled plugin. | `extensions/bonjour/`, `src/infra/bonjour-discovery.ts` |
| **wide-area DNS** | Cross-network discovery: unicast DNS-SD over Tailscale + CoreDNS. | `src/infra/widearea-dns.ts`, `src/cli/dns-cli.ts` |
| **node-host** | The per-node process. Connects to the Gateway WS, declares a command surface, executes forwarded `system.run` etc. | `src/node-host/`, `src/cli/node-cli/` |
| **nodes-screen** | The Control-UI / CLI surface listing fleet nodes. | `ui/src/ui/views/nodes.ts`, `src/cli/nodes-cli/` |

The Gateway WS control plane (`ws://127.0.0.1:18789` by default) is the single
spine. Operators *and* nodes connect to the same port, distinguished by
`role` in the connect handshake (`src/gateway/protocol/schema/frames.ts:41`).

---

## 2. Overall architecture (ASCII)

```
                          T A I L N E T  (WireGuard mesh, Tailscale)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                                                                           │
  │   PHONE (iOS node)            LAPTOP (Gateway host)        SERVER (node)   │
  │  ┌──────────────────┐        ┌───────────────────────┐   ┌──────────────┐ │
  │  │ NWBrowser (mDNS) │        │  GATEWAY PROCESS       │   │ node host    │ │
  │  │ + tailnet DNS-SD │        │  openclaw gateway      │   │ openclaw     │ │
  │  │                  │        │                       │   │   node run   │ │
  │  │ connect(role=    │ ─WS──► │ ┌───────────────────┐ │   │              │ │
  │  │   node)          │        │ │ WS server :18789  │ │ ◄─┼─ connect     │ │
  │  └──────────────────┘        │ │ ws-connection/    │ │   │   (role=node)│ │
  │                              │ │  message-handler  │ │   └──────────────┘ │
  │                              │ └─────────┬─────────┘ │                    │
  │                              │           │           │   advertises:      │
  │                              │   ┌───────▼────────┐  │   _openclaw-gw     │
  │                              │   │ NodeRegistry   │  │     ._tcp          │
  │                              │   │ (in-memory map │  │   ┌──────────────┐ │
  │                              │   │  connId→node)  │  │   │ CoreDNS :53  │ │
  │                              │   └───────┬────────┘  │   │ wide-area    │ │
  │                              │           │           │   │ DNS-SD zone  │ │
  │                              │   ┌───────▼────────┐  │   └──────────────┘ │
  │   AGENT (model + tools) ─────┼──►│ node.invoke    │  │                    │
  │   runs HERE, on the Gateway  │   │ handler        │──┼──► forwards         │
  │                              │   │ (server-       │  │    node.invoke.    │
  │                              │   │  methods/      │  │    request event   │
  │                              │   │  nodes.ts)     │  │    to target node  │
  │                              │   └────────────────┘  │                    │
  │                              │  bonjour plugin        │                   │
  │                              │  advertises beacon ────┼──► mDNS / CoreDNS  │
  │                              └───────────────────────┘                    │
  └───────────────────────────────────────────────────────────────────────────┘

  Discovery inputs feeding a client's "pick a gateway" list:
    1. mDNS  `_openclaw-gw._tcp` on `local.`        (LAN only)
    2. unicast DNS-SD on a configured domain        (cross-network, via Tailscale)
    3. Tailscale MagicDNS name / tailnet IP         (cross-network direct)
    4. SSH tunnel to loopback :18789                (universal fallback)
```

---

## 3. Subsystem detail

### 3.1 The Gateway WS control plane (the spine)

- One Gateway per host, recommended (`docs/gateway/discovery.md:21`). Owns
  sessions, pairing, and the node registry.
- WS server: `src/gateway/server/ws-connection.ts`. Bind mode is
  `gateway.bind` ∈ `auto | lan | loopback | custom | tailnet`
  (`src/config/types.gateway.ts:3`); default loopback.
- **Connect handshake** wire shape — `ConnectParamsSchema`
  (`src/gateway/protocol/schema/frames.ts:20`):
  ```
  connect {
    minProtocol, maxProtocol: int
    client: { id, displayName?, version, platform, deviceFamily?,
              modelIdentifier?, mode, instanceId? }
    caps?: string[]            // capability families, e.g. ["system"]
    commands?: string[]        // declared command surface, e.g. ["system.run"]
    permissions?: { [name]: bool }   // OS permission grants on the node
    pathEnv?: string
    role?: string              // "operator" | "node" | ...
    scopes?: string[]
    device?: { id, publicKey, signature, signedAt, nonce }  // device identity
    auth?:   { token?, bootstrapToken?, deviceToken?, password? }
  }
  ```
- The server sends a `connect.challenge { nonce, ts }` event first
  (`src/gateway/server/ws-connection.ts:300-304`); the client signs that nonce
  into `device.signature`. Connect itself is rejected if it is not the first
  frame (`src/gateway/server-methods/connect.ts`).

### 3.2 NodeRegistry — the in-memory fleet table

`src/gateway/node-registry.ts`. **The fleet's live state is just a `Map`** —
there is no DB for *connected* nodes (durability lives in the pairing stores,
§3.7).

- `NodeSession` (`node-registry.ts:4-23`): `nodeId`, `connId`, `client` (the WS
  socket), `clientMode`, `displayName`, `platform`, `version`, `deviceFamily`,
  `remoteIp`, `caps[]`, `commands[]`, `permissions`, `pathEnv`, `connectedAtMs`.
- `register()` (`node-registry.ts:45`): pulls `nodeId` from
  `connect.device?.id ?? connect.client.id`; indexes by both `nodesById` and
  `nodesByConn`.
- `unregister(connId)` (`node-registry.ts:85`): on socket close, drops the node
  and rejects all its pending invokes.
- `invoke()` (`node-registry.ts:111`): the routing primitive. Sends a
  `node.invoke.request` event to the node's socket, stores a `PendingInvoke`
  keyed by a generated `requestId`, and returns a Promise resolved when the node
  replies with `node.invoke.result` (`handleInvokeResult`, `node-registry.ts:161`)
  or rejected on a 30s default timeout.

### 3.3 node-host — the per-node process

Entry point `src/node-host/runner.ts`, `runNodeHost()` (`runner.ts:189`):

1. `ensureNodeHostConfig()` — loads/creates `~/.openclaw/node.json`
   (`src/node-host/config.ts`): `{ version:1, nodeId (uuid), token?,
   displayName?, gateway:{host,port,tls,tlsFingerprint} }`, written `0o600`.
2. Resolves Gateway auth (`resolveNodeHostGatewayCredentials`, `runner.ts:158`):
   `OPENCLAW_GATEWAY_TOKEN` / `OPENCLAW_GATEWAY_PASSWORD` env first, then config
   `gateway.auth.token/password`. In local mode it deliberately strips
   `gateway.remote.*` so a node never inherits remote-client auth
   (`buildNodeHostLocalAuthConfig`, `runner.ts:175`).
3. Builds a `GatewayClient` (`runner.ts:222`) with `role:"node"`,
   `mode:NODE`, `caps:["system", ...plugin caps]`,
   `commands:[system.run.prepare, system.run, system.which,
   system.execApprovals.get/set, ...plugin commands]`
   (command lists from `src/infra/node-commands.ts`), and a device identity
   (`loadOrCreateDeviceIdentity()`).
4. On every `node.invoke.request` event, runs `handleInvoke()` and replies.
5. `await new Promise(() => {})` — runs forever; `GatewayClient` handles
   reconnect/retry internally. A terminal auth pause exits non-zero so
   launchd/systemd restarts it (`handleNodeHostReconnectPaused`, `runner.ts:70`).

CLI: `openclaw node run` (foreground) / `openclaw node install|start|stop|
restart|uninstall` (service) — `src/cli/node-cli/register.ts`. Flags:
`--host --port --tls --tls-fingerprint --node-id --display-name --runtime`.
A headless node host also **auto-advertises a browser proxy**
(`docs/cli/node.md:31`), so the Gateway can drive a browser on that node.

There is also a `macOS node mode` (the menubar app connects as a node) and an
iOS/Android node, but those are app-side; the headless `node run` is the
cross-platform path an OC port should mirror.

### 3.4 Bonjour / mDNS — LAN discovery

Bundled plugin `extensions/bonjour/`. The plugin (`index.ts`) registers a
**gateway discovery service** via `api.registerGatewayDiscoveryService(...)`;
the Gateway calls `advertise(ctx)` at startup.

- Advertiser: `extensions/bonjour/src/advertiser.ts`,
  `startGatewayBonjourAdvertiser()` (`advertiser.ts:352`). Uses the
  `@homebridge/ciao` mDNS library (`advertiser.ts:106`).
- **Only the Gateway advertises**, service type `_openclaw-gw._tcp` on domain
  `local` (`advertiser.ts:468-476`).
- TXT record built at `advertiser.ts:433-461`:
  ```
  role=gateway   transport=gateway   displayName=<name>
  lanHost=<host>.local   gatewayPort=18789
  gatewayTls=1   gatewayTlsSha256=<sha256>     (only when TLS on)
  canvasPort=<port>                            (when canvas host enabled)
  tailnetDns=<magicdns>   sshPort=<n>   cliPath=<path>   (mDNS "full" mode only)
  ```
- A **watchdog** (`advertiser.ts:656`) re-advertises stuck services and, after
  `MAX_*` restarts, disables the advertiser for the process lifetime.
- Browser/resolver side: `src/infra/bonjour-discovery.ts`.
  `discoverGatewayBeacons()` (`bonjour-discovery.ts:580`) shells out to
  `dns-sd` on macOS / `avahi-browse` on Linux, parses browse + resolve output
  into `GatewayBonjourBeacon` objects.
- Disable knobs: `OPENCLAW_DISABLE_BONJOUR=1`, `discovery.mdns.mode:"off"`,
  `openclaw plugins disable bonjour`. Auto-disables inside detected containers
  (`advertiser.ts:137` `isContainerEnvironment`).

**Limitation, stated explicitly** (`docs/gateway/discovery.md:51`): multicast
mDNS does not cross networks. That is what wide-area DNS solves.

### 3.5 wide-area DNS — cross-network discovery

Same `_openclaw-gw._tcp` DNS-SD service, but published as **unicast** records
in a real DNS zone served over the tailnet. "Wide-Area Bonjour."

- Zone writer: `src/infra/widearea-dns.ts`. `writeWideAreaGatewayZone()`
  (`widearea-dns.ts:167`) renders a BIND zone file to
  `~/.openclaw/dns/<domain>.db` (`getWideAreaZonePath`, `widearea-dns.ts:28`).
  The zone (`renderZone`, `widearea-dns.ts:105`) contains:
  - `SOA` + `NS ns1`, `ns1 A <tailnetIPv4>`, host `A`/`AAAA` → tailnet IPs.
  - `_openclaw-gw._tcp PTR <instance>`, an `SRV 0 0 <gatewayPort> <host>`, and
    a `TXT` record with the same keys as the mDNS beacon.
  - A content hash + auto-incrementing serial so the file only rewrites on
    real change (`computeContentHash`, `extractSerial`).
- The Gateway writes this zone at startup when
  `discovery.wideArea.enabled` is true (`src/gateway/server-discovery-runtime.ts:144`).
- The DNS server itself is **CoreDNS**, set up out-of-band by
  `openclaw dns setup [--apply]` (`src/cli/dns-cli.ts`). `--apply` (macOS +
  Homebrew only) installs CoreDNS, writes a `<domain>.server` stanza that
  `bind`s port 53 to the tailnet IPs and `file`-serves the zone, then restarts
  the `coredns` brew service.
- The user then configures **Tailscale Split DNS** in the Tailscale admin
  console: a nameserver pointing at the Gateway's tailnet IP, restricted to the
  discovery domain. Clients on the tailnet then resolve
  `_openclaw-gw._tcp.<domain>` unicast.
- Client side: `discoverWideAreaViaTailnetDns()` in `bonjour-discovery.ts:324`
  is a *fallback* — it reads `tailscale status --json` for tailnet IPs and
  `dig`s each one for the DNS-SD PTR/SRV/TXT chain, used when normal
  `dns-sd` resolution against the configured domain returns nothing.

Domain default: none. `discovery.wideArea.domain` (or env
`OPENCLAW_WIDE_AREA_DOMAIN`) — e.g. `openclaw.internal`
(`resolveWideAreaDiscoveryDomain`, `widearea-dns.ts:15`).

### 3.6 Tailscale integration

OpenClaw never speaks WireGuard itself — it **shells out to the `tailscale`
CLI**. Two distinct uses:

1. **Detection / hints** (`src/infra/tailscale.ts`):
   - `findTailscaleBinary()` — 4-strategy lookup (PATH, macOS app path, `find`,
     `locate`).
   - `getTailnetHostname()` — parses `tailscale status --json` for the MagicDNS
     name (`Self.DNSName`) or falls back to a tailnet IP. Published as the
     `tailnetDns` TXT hint.
   - `pickPrimaryTailnetIPv4/6()` (`src/infra/tailnet.ts`) — finds the local
     interface address in Tailscale's CGNAT ranges (`100.64.0.0/10`,
     `fd7a:115c:a1e0::/48`). Used for the wide-area zone's A/AAAA records.
   - `readTailscaleWhoisIdentity()` — `tailscale whois --json <ip>` → login
     identity (cached 60s). Used for identity-header auth (below).
2. **Exposure** (`src/gateway/server-tailscale.ts`,
   `startGatewayTailscaleExposure()`): `gateway.tailscale.mode` ∈
   `off | serve | funnel`:
   - `serve` → `tailscale serve --bg --yes <port>` — tailnet-only HTTPS,
     Gateway stays on loopback, Tailscale injects identity headers.
   - `funnel` → `tailscale funnel ...` — public HTTPS; OpenClaw refuses unless
     `gateway.auth.mode == password` (`docs/gateway/tailscale.md:120`).
   - `resetOnExit` undoes serve/funnel on shutdown.
   - Alternatively `gateway.bind:"tailnet"` binds the WS directly to the
     tailnet IP (no Serve, no HTTPS).

`tailscale` CLI must be installed and logged in; Funnel needs Tailscale
v1.38.3+, MagicDNS, HTTPS enabled, and only ports 443/8443/10000.

### 3.7 Pairing & the two pairing stores

A node becomes *trusted* via pairing. There are **two** stores (this trips
people up — `docs/nodes/index.md:52`):

- **Device pairing** — the durable, authoritative store. WS nodes present a
  `device` identity in `connect`; the Gateway creates a pending pairing request
  for `role:node`. Approved via `openclaw devices list/approve/reject`. The
  device-pairing record is the **durable approved-role contract** — token
  rotation stays inside it; it cannot upgrade a node to a role pairing never
  granted. Handshake processing: `src/gateway/server/ws-connection/
  message-handler.ts` (`requirePairing`, line ~919; device-signature checks
  ~lines 725-785).
- **Node pairing** (`node.pair.*`, CLI `openclaw nodes pending/approve/
  reject/remove/rename`) — `src/infra/node-pairing.ts`. A separate
  Gateway-owned store of declared node surfaces + a pairing token. It does
  **not** gate the WS connect handshake; it is the metadata/rename store
  feeding the nodes-screen.

### 3.8 nodes-screen — the fleet UI/CLI surface

- **Control UI**: `ui/src/ui/views/nodes.ts`, `renderNodes()`. Three cards:
  Exec Approvals, Exec-Node Binding, **Devices** (pending/paired pairing
  requests, role tokens with rotate/revoke), and **Nodes** (each node: title,
  `nodeId`, `remoteIp`, version, chips for paired/connected + caps + commands).
  `renderNode()` at `nodes.ts:439`.
- **Node-list aggregation**: `src/gateway/node-catalog.ts`,
  `createKnownNodeCatalog()` (`node-catalog.ts:178`) merges three sources —
  paired devices, paired nodes, and live `NodeSession`s — into one
  `NodeListNode` per `nodeId` with `paired`/`connected` flags and an effective
  last-seen. Served by the `node.list` / `node.describe` RPCs
  (`src/gateway/server-methods/nodes.ts:719`).
- **CLI**: `src/cli/nodes-cli/` — `openclaw nodes status|describe|invoke|
  canvas|camera|screen|location|notify|pending|approve|reject|remove|rename`.
- **Discovery CLI**: `openclaw gateway discover`
  (`src/cli/gateway-cli/discover.ts` + `register.ts:620`) — runs
  `discoverGatewayBeacons()` and prints each beacon with ws/ssh targets.

---

## 4. End-to-end flows

### 4.1 A node joins the fleet

1. Operator runs `openclaw node run --host <gw> --port 18789` (or `node install`).
2. node-host loads/creates `~/.openclaw/node.json` (random `nodeId` if new) and
   a device identity keypair.
3. node-host opens a WS to the Gateway, receives `connect.challenge`, signs the
   nonce, and sends `connect { role:"node", device:{...}, caps, commands,
   auth:{token|password} }`.
4. Gateway's `message-handler` verifies shared-secret auth + device signature.
   If the device is unpaired, it creates a **pending device-pairing request**
   and the connect fails with `NOT_PAIRED`.
5. Operator approves: `openclaw devices approve <requestId>`. (Or, if the
   operator opted in, the trusted-CIDR path auto-approves — §5.)
6. node-host's `GatewayClient` retries; this time connect succeeds. The Gateway
   calls `NodeRegistry.register()` → the node is now a live `NodeSession`.
7. The node is now visible in `openclaw nodes status` and the Control-UI
   nodes-screen (connected=true).

### 4.2 Discovery: how a client *finds* the Gateway

Client transport-selection policy (`docs/gateway/discovery.md:126`):

1. If a paired direct endpoint is configured + reachable → use it.
2. Else browse mDNS `_openclaw-gw._tcp` on `local.` **and** the configured
   wide-area domain → offer a "use this gateway" pick, save it.
3. Else if a tailnet MagicDNS/IP is configured → connect direct.
4. Else → SSH tunnel to loopback `:18789`.

Routing must use the **resolved SRV+A/AAAA endpoint**, never the unauthenticated
TXT hints (`lanHost`, `tailnetDns`, `gatewayPort`) — TXT is UX-only
(`docs/gateway/bonjour.md:112`).

### 4.3 Request routing: node → Gateway → another node

The agent runs on the Gateway. When a tool call targets a node (e.g. `exec`
with `host=node`):

1. Operator/agent issues RPC `node.invoke { nodeId, command, params?,
   timeoutMs?, idempotencyKey }` (`NodeInvokeParamsSchema`,
   `src/gateway/protocol/schema/nodes.ts:106`).
2. `node.invoke` handler (`src/gateway/server-methods/nodes.ts:886`):
   - If the node is **not connected**, try to wake it: APNs push for iOS
     (`maybeWakeNodeWithApns`), wait for reconnect, retry, then a user-facing
     "reopen" nudge. If still absent → `UNAVAILABLE { code:NOT_CONNECTED }`.
   - **Two policy gates** (`docs/nodes/index.md:193`): (a) the command must be
     in the node's declared `connect.commands`; (b) the Gateway's platform
     policy allowlist must permit it (`isNodeCommandAllowed` +
     `resolveNodeCommandAllowlist`, `src/gateway/node-command-policy.ts`).
     `system.execApprovals.*` and persistent `browser.proxy` mutations are
     hard-blocked on this path.
   - Params are sanitized (`sanitizeNodeInvokeParamsForForwarding`); a
     plugin-owned node-invoke policy may run (`applyPluginNodeInvokePolicy`).
3. `NodeRegistry.invoke()` sends a `node.invoke.request` event over the target
   node's WS socket and awaits its `node.invoke.result`.
4. Result (or `TIMEOUT`/`NOT_CONNECTED`/`UNAVAILABLE`) flows back to the caller.

So phone→server is: phone's operator app → Gateway RPC `node.invoke` →
Gateway forwards `node.invoke.request` event → server node-host executes →
result event → Gateway → phone. The Gateway is the only router; nodes are
never peers.

---

## 5. Auth / trust model

- **Mesh layer**: Tailscale (WireGuard) provides device-to-device encryption +
  ACLs. OpenClaw treats the tailnet as a *trusted private network* but still
  requires app-layer pairing on top.
- **Transport auth** to the Gateway WS — `gateway.auth.mode`:
  `none | token | password | trusted-proxy`. Non-loopback binds require a real
  auth path (`docs/network.md:21`).
- **Node identity**: each node has an Ed25519-style device keypair. Connect
  sends `device:{id,publicKey,signature,signedAt,nonce}`; the Gateway verifies
  the signature over a versioned payload (`buildDeviceAuthPayloadV3`,
  `src/gateway/device-auth.ts:36`) and that `deviceId` derives from the public
  key. `signedAt` staleness and nonce are checked
  (`message-handler.ts:746-769`).
- **Pairing is the trust grant**: an unpaired node's connect fails closed with
  `NOT_PAIRED`. A human approves a pending request; the device-pairing record
  is the durable approved-role contract.
- **Trusted-CIDR auto-approve** (opt-in, default off):
  `gateway.nodes.pairing.autoApproveCidrs` — only fresh `role:node` pairings
  with **no requested scopes**, from a matching CIDR, are auto-approved
  (`shouldAutoApproveNodePairingFromTrustedCidrs`,
  `src/gateway/node-pairing-auto-approve.ts:30`). Operator/Control-UI/WebChat
  and any role/scope/key upgrade still need manual approval.
- **Tailscale identity-header auth** (Serve mode only): with
  `gateway.auth.allowTailscale:true`, Control-UI/WS auth can use the
  `tailscale-user-login` header, verified by resolving `x-forwarded-for` via
  `tailscale whois` (`docs/gateway/tailscale.md:35`). Does **not** apply to
  HTTP API endpoints or node-role connections — nodes always do normal pairing.
- **TXT records are unauthenticated**: clients must not trust mDNS/DNS-SD TXT
  for routing or TLS pins; a discovered `gatewayTlsSha256` must never override
  a stored pin (`docs/gateway/bonjour.md:110-114`).
- **Exec approvals are per-node-host**, enforced locally on the node at
  `~/.openclaw/exec-approvals.json` — the Gateway forwards a stored canonical
  `systemRunPlan`, not later-edited fields (`docs/cli/node.md:165`).
- **Insecure private WS opt-in**: a node connecting to a non-loopback plaintext
  `ws://` Gateway must set `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1`, else startup
  fails closed (`docs/cli/node.md:81`).

---

## 6. Config surface

Gateway `~/.openclaw/openclaw.json` (types: `src/config/types.gateway.ts`):

```json5
{
  gateway: {
    bind: "auto",                 // auto|lan|loopback|custom|tailnet
    auth: { mode: "token", token: "..." },
    tls: { enabled: false },
    tailscale: { mode: "off", resetOnExit: false },   // off|serve|funnel
    nodes: {
      allowCommands: [],          // opt-in dangerous node commands
      denyCommands: [],           // always wins
      pairing: { autoApproveCidrs: [] }   // trusted-CIDR auto-approve
    }
  },
  discovery: {
    mdns:     { mode: "minimal" },           // off|minimal|full
    wideArea: { enabled: false, domain: "openclaw.internal" }
  },
  tools: { exec: { host: "node", security: "allowlist", node: "<id-or-name>" } }
}
```

Node `~/.openclaw/node.json` (per node, written `0o600`): `{ version, nodeId,
token?, displayName?, gateway:{host,port,tls,tlsFingerprint} }`.

Relevant env vars: `OPENCLAW_GATEWAY_TOKEN`, `OPENCLAW_GATEWAY_PASSWORD`,
`OPENCLAW_DISABLE_BONJOUR`, `OPENCLAW_WIDE_AREA_DOMAIN`, `OPENCLAW_TAILNET_DNS`,
`OPENCLAW_MDNS_HOSTNAME`, `OPENCLAW_SSH_PORT`, `OPENCLAW_CLI_PATH`,
`OPENCLAW_ALLOW_INSECURE_PRIVATE_WS`,
`OPENCLAW_GATEWAY_DISCOVERY_ADVERTISE_TIMEOUT_MS`.

To "join a fleet" a user: (1) installs Tailscale on every device and joins one
tailnet; (2) runs the Gateway on one device with `gateway.bind:"tailnet"` or
`tailscale.mode:"serve"`; (3) optionally runs `openclaw dns setup --apply` +
Tailscale Split DNS for wide-area discovery; (4) runs `openclaw node install`
on each other device pointed at the Gateway; (5) approves each node with
`openclaw devices approve`.

---

## 7. Dependencies

- **Tailscale**: an external binary the user must install and log in. OpenClaw
  shells out; it bundles no WireGuard. Funnel/Serve are Tailscale-hosted
  features. There is **no OpenClaw-hosted relay/DNS service** — the wide-area
  DNS server (CoreDNS) runs on the user's own Gateway host.
- **CoreDNS**: external; installed via Homebrew by `openclaw dns setup --apply`
  (macOS only today). Any DNS-SD-capable server would work; the zone file is
  plain BIND format.
- **`@homebridge/ciao`**: npm mDNS library, the only third-party lib doing real
  protocol work (Bonjour advertising). Imported lazily by the bonjour plugin.
- **`dns-sd`** (macOS, built-in) / **`avahi-browse`** (Linux) / **`dig`**:
  external CLI tools shelled out for mDNS browsing + unicast DNS-SD probing.
- **SSH**: the universal fallback transport; no special server, just sshd.
- **APNs**: Apple Push, used only to wake sleeping iOS nodes; not needed for
  laptop/server nodes.

Net: the only hard external dependency for cross-network fleet routing is
**Tailscale**. mDNS adds a tiny npm lib. Wide-area DNS adds CoreDNS but is
optional (Tailscale MagicDNS + a stored endpoint also work).

---

## 8. What an OC port would need

OC today (`opencomputer/gateway/`) has a single-host gateway: `server.py` runs
channel adapters plus an optional **wire-mode** WebSocket server
(`wire_server.py`, `protocol_v2.py`). There is `cli_pair.py` for **DM** pairing
(messaging-channel user pairing) but **no device/node concept, no node
registry, no discovery, no Tailscale, no DNS-SD**. The agent loop, dispatch,
and outgoing queue are all single-machine.

### Maps onto OC's existing gateway (extend, don't invent)

- **WS spine** — OC's `wire_server.py` is the analogue of OpenClaw's WS server.
  A port adds a `role` field to the wire-protocol connect frame
  (`protocol_v2.py`) and a `node` role alongside the existing client roles.
- **Pairing** — OC's `cli_pair.py` + DM pairing store is structurally similar
  to OpenClaw's pairing stores; a device/node pairing store can be modeled on
  it (pending → approved, durable approved-role contract). The 28-item
  reference block and prior pairing work give OC a pairing scaffold to extend.
- **`exec`/tool routing** — OC's `dispatch.py` / `agent_router.py` /
  `binding_resolver.py` is where a `host=node` selector and the
  `node.invoke`-equivalent forward would slot in. `binding_resolver.py`
  already resolves *which agent*; node-binding is the same shape applied to
  *which machine*.
- **CLI** — `cli_gateway.py` gains `discover`; a new `cli_node.py` mirrors
  `openclaw node run/install/...`; `cli_pair.py` or a new devices CLI gains
  node approve/reject.
- **Doctor** — OC's `doctor.py` is the natural home for fleet/discovery health
  checks (mirror OpenClaw's bonjour log diagnostics).

### Genuinely new in OC (must be built from scratch)

1. **NodeRegistry** — an in-memory `connId → NodeSession` map plus a
   `node.invoke` request/response correlator with per-invoke timeout. Direct
   port of `node-registry.ts`; ~200 lines, no external deps.
2. **node-host process** — a new long-running OC process/CLI
   (`opencomputer node run`) that connects *into* a remote gateway's wire
   server as `role=node`, declares a command surface, and executes forwarded
   requests (start with a Python `system.run` equivalent gated by exec
   approvals). New `node.json` per-node config file.
3. **`node.invoke` routing handler** — gateway-side method that applies the
   two-gate command policy (declared-commands ∩ platform allowlist) and
   forwards to the registry. New.
4. **Node command policy + allowlist** — `allowCommands`/`denyCommands` config
   + per-platform default allowlist. New.
5. **Device identity + signature** — Ed25519 keypair per node, challenge-nonce
   signing in the connect handshake. OC has DM pairing but not cryptographic
   device identity; new.
6. **Bonjour/mDNS advertiser + browser** — a Python equivalent of the
   `ciao`-based advertiser (e.g. `zeroconf`) advertising `_opencomputer-gw._tcp`
   with the TXT schema, plus a `dns-sd`/`avahi-browse` browser. New, optional,
   default-off off-macOS (mirror OpenClaw's container auto-disable).
7. **Wide-area DNS zone writer + `oc dns setup`** — render a BIND zone to
   `~/.opencomputer/dns/<domain>.db`, wire CoreDNS + Tailscale Split DNS. New,
   optional. Port `widearea-dns.ts` largely verbatim (it is pure string work).
8. **Tailscale integration** — `findTailscaleBinary` + `tailscale status/whois`
   parsing + optional `tailscale serve/funnel` exposure. New; pure subprocess
   wrappers, no library.
9. **nodes-screen** — a CLI `oc nodes status/describe/invoke` and (if OC grows
   a web UI) a fleet view. New; the `node-catalog.ts` merge logic (paired ∪
   live) is the part to copy.
10. **Node wake** (APNs) — only needed if OC ever ships an iOS node; safely
    deferred. Laptop/server nodes never sleep-disconnect the way phones do.

### Recommended port slice, in dependency order

`NodeRegistry` + `role=node` connect → `node.invoke` handler + command policy →
node-host process + `oc node` CLI → device identity/pairing → `oc nodes` CLI /
nodes-screen → Tailscale wrappers + `gateway.bind:tailnet` → Bonjour plugin →
wide-area DNS + `oc dns setup`. The first four deliver a working
single-tailnet fleet (manual endpoint config); discovery and Bonjour are pure
UX sugar layered on top.

### Notable surprises / gotchas

- OpenClaw's "fleet" is **not a mesh** at the app layer — it is a strict star
  through one Gateway. The mesh is entirely Tailscale's job. An OC port should
  resist the temptation to build node-to-node routing.
- Live fleet state is **not persisted** — `NodeRegistry` is a plain `Map`.
  Durability lives only in the pairing stores. A node that disconnects simply
  vanishes from `listConnected()`.
- There are **two** pairing stores (device pairing = authoritative gate; node
  pairing = metadata/rename store). Easy to conflate; OC can collapse them into
  one if starting fresh, but be deliberate about it.
- Discovery never relaxes transport security: a discovered tailnet IP is a
  *routing hint*, not permission for plaintext `ws://`
  (`docs/gateway/discovery.md:117`).
- `gateway discover` exists as a CLI but there is **no automatic** "connect to
  the discovered gateway" — discovery only produces a pick-list; the human or a
  client policy chooses and stores the endpoint.
- The legacy TCP "bridge protocol" for nodes is **removed** from current builds
  (`docs/gateway/discovery.md:26`) — do not port it; WS is the only node
  transport.
```
