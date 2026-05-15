# Bundle MCP — Shipping an MCP Server With Your Plugin

> **Status:** Shipped 2026-05-15. See `docs/plans/mcp-openclaw-port.md` for the
> design plan. Reference implementation: `extensions/downloads-cleanup-mcp/`.

Plugins can ship their own [Model Context Protocol](https://modelcontextprotocol.io)
servers as part of the plugin tree. OC mounts the server's tools alongside
user-configured MCPs under the namespaced prefix
`<plugin_id>__<server_name>__<tool>`. Two plugins shipping a server named
`github` get distinct namespaces by construction — no collision suffix needed.

This guide walks through the authoring surface, the safety model, and the
lifecycle.

---

## Why bundle an MCP server with your plugin?

The whole point of bundling MCP is **language independence**. A Python OC
plugin can ship:

- A Node.js MCP server (you have a TypeScript-only library to expose).
- A Go MCP server (the upstream API speaks Go and the Python wrapper is
  thin).
- A Rust MCP server (you need the performance).
- A Python MCP server in the same tree (the simplest case — illustrated by
  `extensions/downloads-cleanup-mcp/`).

The MCP server is a separate subprocess. Your plugin's Python is free to
declare other tools, register channels, etc. — bundling MCP is additive.

---

## Manifest declaration

Add a `bundle_mcp` array to your `plugin.json`:

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "entry": "plugin",
  "bundle_mcp": [
    {
      "name": "main",
      "transport": "stdio",
      "command": "${PLUGIN_ROOT}/mcp_server.py",
      "args": [],
      "env": {},
      "lazy": true,
      "osv_check": false
    }
  ]
}
```

Field reference (matches `plugin_sdk.BundleMcpServer`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | Becomes `<plugin_id>__<name>` in the global MCP registry. Alphanumeric / dash / underscore. |
| `transport` | `stdio`\|`sse`\|`http` | `stdio` | Same shape as `MCPServerConfig`. |
| `command` | string | `""` | stdio only. Supports `${PLUGIN_ROOT}` placeholder. |
| `args` | list[string] | `[]` | stdio only. Supports `${PLUGIN_ROOT}` placeholder in each item. |
| `env` | dict[string,string] | `{}` | stdio only. Values support `${PLUGIN_ROOT}` placeholder. |
| `cwd` | string | `""` | Working dir; supports `${PLUGIN_ROOT}` placeholder. Empty = plugin root. |
| `url` | string | `""` | sse/http only. Endpoint URL. |
| `headers` | dict[string,string] | `{}` | sse/http only. HTTP headers (auth). |
| `connection_timeout_seconds` | float | `30.0` | Initial-connect timeout. |
| `lazy` | bool | `true` | If `true`, subprocess spawns on first tool call. If `false`, spawns at plugin activation. |
| `tools_allow` | list[string]\|null | `null` | Per-server tool whitelist. `null` = all. |
| `tools_deny` | list[string] | `[]` | Per-server tool blacklist (applied after `tools_allow`). |
| `osv_check` | bool | `true` | Run an OSV malware scan on the package before spawn. Plugins shipped from trusted sources can set `false`. |

---

## The `${PLUGIN_ROOT}` placeholder

Inside `command`, `args`, `env` values, and `cwd`, the literal token
`${PLUGIN_ROOT}` is substituted with the plugin's on-disk absolute path
at MCP-spawn time.

Examples:

```jsonc
{
  "command": "${PLUGIN_ROOT}/bin/server",   // plugin-bundled binary
  "args": ["--config", "${PLUGIN_ROOT}/config/default.yaml"],
  "env": {
    "DATA_DIR": "${PLUGIN_ROOT}/data"
  }
}
```

**Safety: path-escape attacks are refused.** If the substituted `command`
contains a path separator AND the resolved absolute path would land
OUTSIDE the plugin tree, OC raises `BundleMcpSafetyError` and skips the
bundle entry (logged at WARNING; the rest of the plugin still loads). So
this manifest:

```jsonc
{ "command": "${PLUGIN_ROOT}/../../../usr/bin/rm" }
```

…is refused. Absolute paths NOT relative to `${PLUGIN_ROOT}` are allowed
(`/usr/bin/python3`, etc.) — the plugin author explicitly chose a system
binary, and the runtime env whitelist on spawn is the safety layer for
those.

> **Differs from OpenClaw:** their tool uses `${CLAUDE_PLUGIN_ROOT}`. OC
> uses `${PLUGIN_ROOT}` so the placeholder is OC-native. A plugin that
> wants to target both can declare both literal placeholders verbatim
> (each tool substitutes its own token).

---

## Lazy vs eager spawn

`lazy: true` (default) registers the bundle in OC's process-global
`BundleMcpRegistry` but produces an `MCPServerConfig` with
`enabled=False`. The MCPManager's `connect_all` skips disabled
servers, so a lazy bundle does NOT auto-mount at chat start. This
keeps `oc chat` cold-start time uncoupled from how many bundled-MCP
plugins are installed.

`lazy: false` (eager opt-in) produces `enabled=True` — the MCPManager
spawns the subprocess at `connect_all` time alongside user-configured
servers. Choose this when:

- The MCP server is fast to start AND needed immediately.
- Eager registration gives meaningfully better UX (e.g. tab-completion
  of tool names at chat startup).

**M1 limitation:** waking a lazy bundle currently requires explicit
operator action — `oc mcp enable <plugin_id>__<server_name>` followed
by `oc mcp reconnect` (or restarting `oc chat`). The plan
(`docs/plans/mcp-openclaw-port.md`, follow-up M1.A) calls for adding
**first-tool-call wakeup** so the agent can transparently mount a
lazy bundle on demand when it routes the first invocation to its
namespace. Until that lands, prefer `lazy: false` for any bundle whose
tools you want immediately accessible without operator intervention.

The default `lazy: true` choice keeps plugin install ergonomically
cheap — bundle MCPs don't slow down chat startup. The cost is the
manual wake step; the trade-off makes sense for plugins that ship
several rarely-used MCP servers.

---

## Lifecycle

```
plugin activated
   └─ loader._register_bundle_mcps(candidate)
        └─ for each entry in manifest.bundle_mcp:
             • expand ${PLUGIN_ROOT} in command / args / env / cwd
             • assert command stays inside plugin root (raises on escape)
             • produce MCPServerConfig(name="<plugin_id>__<server.name>",
                                       enabled=(not server.lazy))
             • register in BundleMcpRegistry keyed by plugin_id

agent starts (oc chat / oc gateway / etc.)
   └─ MCPManager.connect_all (include_bundle=True)
        • merge user-configured servers + default_registry.all_server_configs()
        • filter to enabled=True — lazy bundles are skipped
        • for each remaining server: connect via stdio/sse/http
        • register tools as <plugin_id>__<server>__<tool>

plugin deactivated / unloaded
   └─ loader._unregister_bundle_mcps(plugin_id)
        • removes every config for that plugin from the registry
        • subsequent reconnects no longer mount this plugin's bundles
```

The MCPManager handles SIGTERM/SIGKILL of the actual subprocess via
its own connect/disconnect cycle.

---

## CLI surface

```bash
# All MCPs (user-configured + bundled) grouped by section
oc mcp list

# Bundle MCPs only, grouped by plugin
oc mcp bundles
```

`oc mcp bundles` is the focused view — useful when you're authoring
or debugging a plugin and want to see only its bundled servers.

---

## Reference plugin

See `extensions/downloads-cleanup-mcp/` for a working reference:

- `plugin.json` — manifest with `bundle_mcp` declaration.
- `plugin.py` — minimal entry (no runtime tool registrations, since the
  bundle MCP IS the surface).
- `mcp_server.py` — a real, complete Python-based MCP server with three
  tools that touch the user filesystem (`list_downloads`, `summarise_downloads`,
  `archive_old`) and includes a strict scope guard so the server can
  never read or write outside `~/Downloads`.

The reference also demonstrates the test surface in
`tests/test_downloads_cleanup_mcp_plugin.py` — discovery roundtrip +
registry registration + tool listing — without spawning the actual
subprocess.

---

## When to use bundle MCP vs `mcp_servers` field

| Use bundle MCP | Use `mcp_servers` |
|---|---|
| Your plugin SHIPS an MCP server it owns. | Your plugin needs a user-configured preset (e.g. `github`, `filesystem`). |
| The server's lifecycle is bound to the plugin's. | The server runs independently of your plugin. |
| Tools are namespaced `<plugin_id>__<server>__*`. | Tools are namespaced `<server>__*`. |

The two paths coexist: a plugin can declare BOTH a `bundle_mcp` entry
AND `mcp_servers` preset references — they go to different registries
and have different lifecycle owners.

---

## Testing your bundle

The simplest end-to-end smoke test (without spawning the subprocess):

```python
from opencomputer.plugins.discovery import _parse_manifest, PluginCandidate
from opencomputer.plugins.loader import _register_bundle_mcps
from opencomputer.mcp.bundle import BundleMcpRegistry

# Parse your plugin's manifest
manifest = _parse_manifest(plugin_root / "plugin.json")
assert manifest.bundle_mcp  # non-empty

# Register into an isolated registry (production uses default_registry)
reg = BundleMcpRegistry()
cand = PluginCandidate(
    manifest=manifest,
    root_dir=plugin_root,
    manifest_path=plugin_root / "plugin.json",
)
n = _register_bundle_mcps(cand, registry=reg)
assert n >= 1

configs = reg.all_server_configs()
# Verify the namespacing + placeholder expansion happened
assert any(c.name.startswith(manifest.id + "__") for c in configs)
```

For live spawn smoke testing, run `oc chat` with the plugin enabled and
call a tool from your bundle — first call triggers the spawn (lazy
default), subsequent calls reuse the existing subprocess.
