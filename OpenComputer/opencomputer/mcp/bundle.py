"""Bundle MCP — plugin-shipped MCP servers (M1 of mcp-openclaw-port plan).

Plugins can declare ``bundle_mcp: tuple[BundleMcpServer, ...]`` on their
manifest to ship one or more MCP servers as part of the plugin tree.
This module owns:

1. ``${PLUGIN_ROOT}`` placeholder substitution + path-escape safety check.
2. Conversion from :class:`plugin_sdk.core.BundleMcpServer` to the
   internal :class:`opencomputer.agent.config.MCPServerConfig` shape
   that :class:`opencomputer.mcp.client.MCPManager` already knows how
   to connect.
3. The process-global :class:`BundleMcpRegistry` keyed by ``plugin_id``
   so the plugin loader can register on activate and tear down on
   disable; the MCP manager queries this at connect time to mount
   every bundled server alongside user-configured ones.

The plan: docs/plans/mcp-openclaw-port.md (Milestone 1).

Naming: every bundle server's effective ``MCPServerConfig.name`` is
``<plugin_id>__<server.name>`` so tools register as
``<plugin_id>__<server>__<tool>``. Two plugins shipping a server named
``github`` get distinct namespaces by construction — no collision
suffix needed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Final

from opencomputer.agent.config import MCPServerConfig
from plugin_sdk.core import BundleMcpServer

logger = logging.getLogger("opencomputer.mcp.bundle")

#: The single placeholder we substitute inside bundle-MCP command / args /
#: env / cwd values. OpenClaw uses ``${CLAUDE_PLUGIN_ROOT}``; we keep
#: OC's surface OC-native. A plugin that wants both tools can declare
#: both placeholder literals verbatim (each tool substitutes its own).
PLUGIN_ROOT_PLACEHOLDER: Final[str] = "${PLUGIN_ROOT}"

#: Transports the bundle layer accepts. Mirrors :class:`MCPServerConfig`
#: but kept as a frozenset so we can validate without importing dataclass
#: internals.
_VALID_TRANSPORTS: Final[frozenset[str]] = frozenset({"stdio", "sse", "http"})


class BundleMcpSafetyError(RuntimeError):
    """Raised when a bundle MCP config is unsafe to launch.

    Today this fires for:

    * ``${PLUGIN_ROOT}``-relative commands that resolve OUTSIDE the
      plugin root (path-escape attack).
    * Transport literal that's neither ``stdio``, ``sse``, nor ``http``.

    Surfaces as a ``WARNING``-level log in the loader; the plugin
    continues to load but its bundle MCP is skipped — one bad bundle
    entry must not break the rest.
    """


def expand_plugin_root_placeholder(value: str, plugin_root: Path) -> str:
    """Substitute every ``${PLUGIN_ROOT}`` token in ``value``.

    No-op when the token doesn't appear. Returns a plain string so the
    caller can feed the result to anything that takes a string (env
    dict, argv list, cwd path).
    """
    if PLUGIN_ROOT_PLACEHOLDER not in value:
        return value
    return value.replace(PLUGIN_ROOT_PLACEHOLDER, str(plugin_root))


def resolve_bundle_command(
    server: BundleMcpServer, plugin_root: Path,
) -> str:
    """Resolve the executable for a stdio bundle MCP server.

    Returns the expanded command string. For non-stdio transports this
    is the empty string (the connection uses ``url`` instead).

    Safety rule for stdio: when the expanded command contains a path
    separator (i.e. the plugin author meant a filesystem path, not a
    PATH lookup), the resolved absolute path MUST sit inside
    ``plugin_root``. A ``${PLUGIN_ROOT}/../../etc/passwd`` style attack
    escape raises :class:`BundleMcpSafetyError`.

    Bare names (``npx``, ``uvx``, ``python3``) and absolute paths
    outside the plugin tree (``/usr/bin/python3``, ``/opt/foo/bin/x``)
    are returned verbatim — they're not the bundle's responsibility to
    sandbox. The runtime-level env whitelist + install-time OSV scan
    are the security layers for those.
    """
    if server.transport != "stdio":
        return ""
    if not server.command:
        return ""
    expanded = expand_plugin_root_placeholder(server.command, plugin_root)
    # Path-y? Anchor + resolve + check it's still inside the plugin root.
    if "/" in expanded or "\\" in expanded:
        # Absolute path NOT relative to plugin_root → trust it (the
        # plugin author explicitly aimed at a system binary).
        absolute = Path(expanded)
        if not absolute.is_absolute():
            absolute = (plugin_root / expanded).resolve()
        else:
            absolute = absolute.resolve()
        # If the original placeholder was set, enforce containment.
        if PLUGIN_ROOT_PLACEHOLDER in server.command:
            try:
                _ = absolute.relative_to(plugin_root.resolve())
            except ValueError as e:
                raise BundleMcpSafetyError(
                    f"bundle MCP command {server.command!r} escapes plugin "
                    f"root {plugin_root!s}: resolved to {absolute!s}"
                ) from e
        return str(absolute)
    # Bare name — PATH lookup, return verbatim.
    return expanded


def bundle_mcp_to_mcp_server_config(
    plugin_id: str, server: BundleMcpServer, plugin_root: Path,
) -> MCPServerConfig:
    """Produce an :class:`MCPServerConfig` from a bundle entry.

    The resulting config's ``name`` is ``<plugin_id>__<server.name>``
    so subsequent tool registrations land at
    ``<plugin_id>__<server>__<tool>`` — guaranteed not to collide with
    user-configured presets (which never use a double underscore in
    their natural name).

    ``lazy`` semantics in M1: when ``server.lazy`` is True (the default),
    the produced config has ``enabled=False`` — the MCPManager skips it
    in ``connect_all`` so plugin activation never blocks on the bundle
    spawning. Users wake a lazy bundle by either explicit ``oc mcp
    enable <name>`` (then ``oc mcp reconnect``) or by setting
    ``lazy: false`` in the plugin manifest for eager-mount bundles.
    Future M1.A work will add first-tool-call wakeup so the agent can
    transparently mount a lazy bundle on demand without operator
    intervention.
    """
    if server.transport not in _VALID_TRANSPORTS:
        raise BundleMcpSafetyError(
            f"bundle MCP {plugin_id}__{server.name}: invalid transport "
            f"{server.transport!r}, must be one of {sorted(_VALID_TRANSPORTS)}"
        )
    name = f"{plugin_id}__{server.name}"
    resolved_command = resolve_bundle_command(server, plugin_root)
    expanded_args = tuple(
        expand_plugin_root_placeholder(arg, plugin_root) for arg in server.args
    )
    expanded_env = {
        k: expand_plugin_root_placeholder(v, plugin_root)
        for k, v in server.env.items()
    }
    return MCPServerConfig(
        name=name,
        transport=server.transport,
        command=resolved_command,
        args=expanded_args,
        url=server.url,
        env=expanded_env,
        headers=dict(server.headers),
        # lazy=True → enabled=False so connect_all skips it (no spawn at
        # chat start). lazy=False (opt-in) → enabled=True (eager spawn).
        enabled=not server.lazy,
        tools_allow=server.tools_allow,
        tools_deny=server.tools_deny,
        prompts_enabled=True,
        resources_enabled=True,
        timeout=30.0,
        connect_timeout=server.connection_timeout_seconds,
    )


class BundleMcpRegistry:
    """Process-global registry of bundle-MCP server configs keyed by plugin id.

    Thread-safe under a coarse RLock — register / unregister are rare
    (plugin activation / deactivation) and the membership reads from
    :class:`opencomputer.mcp.client.MCPManager` use snapshot semantics
    (a list copy under the lock).

    Each plugin can register multiple servers; the registry stores them
    grouped so :meth:`unregister_plugin` removes exactly that plugin's
    entries even when other plugins also bundle MCPs.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._servers: dict[str, tuple[MCPServerConfig, ...]] = {}

    def register_plugin_servers(
        self,
        plugin_id: str,
        plugin_root: Path,
        servers: tuple[BundleMcpServer, ...],
    ) -> int:
        """Register a plugin's bundle MCP servers — returns count accepted.

        Replaces any prior registration for this plugin id (idempotent
        on live-reload). One bad server (path-escape, invalid transport)
        is logged and skipped; the rest of the plugin's bundle still
        registers — no all-or-nothing failure mode.
        """
        if not servers:
            with self._lock:
                self._servers.pop(plugin_id, None)
            return 0
        accepted: list[MCPServerConfig] = []
        for server in servers:
            try:
                cfg = bundle_mcp_to_mcp_server_config(
                    plugin_id, server, plugin_root,
                )
            except BundleMcpSafetyError as exc:
                logger.warning(
                    "bundle MCP %s__%s rejected: %s",
                    plugin_id, server.name, exc,
                )
                continue
            accepted.append(cfg)
        with self._lock:
            self._servers[plugin_id] = tuple(accepted)
        return len(accepted)

    def unregister_plugin(self, plugin_id: str) -> int:
        """Remove all bundle MCPs registered for ``plugin_id``.

        Returns the count removed (zero when the plugin id was never
        registered). The MCP manager handles process-shutdown of the
        actual subprocesses separately via ``MCPManager.shutdown``;
        :class:`BundleMcpRegistry` only tracks the *config* layer.
        """
        with self._lock:
            removed = self._servers.pop(plugin_id, ())
        return len(removed)

    def servers_for_plugin(
        self, plugin_id: str,
    ) -> tuple[MCPServerConfig, ...]:
        """Return the bundle-MCP configs registered for one plugin.

        Empty tuple for unknown plugin ids. Used by the CLI
        (``oc mcp bundles``) to list bundles grouped by plugin.
        """
        with self._lock:
            return self._servers.get(plugin_id, ())

    def all_server_configs(self) -> list[MCPServerConfig]:
        """Flat list of every registered bundle-MCP config.

        Used by :meth:`opencomputer.mcp.client.MCPManager.connect_all_sync`
        to include bundle servers alongside user-configured ones.
        """
        with self._lock:
            return [cfg for configs in self._servers.values() for cfg in configs]

    def plugin_ids(self) -> list[str]:
        """List plugin ids that have at least one registered bundle MCP."""
        with self._lock:
            return list(self._servers.keys())

    def clear(self) -> None:
        """Drop every registration. Test fixtures only — production uses
        :meth:`unregister_plugin` so callers stay precise about what's
        being removed.
        """
        with self._lock:
            self._servers.clear()

    def replace_config(
        self, plugin_id: str, server_name: str, new_cfg: MCPServerConfig,
    ) -> bool:
        """Swap one server's config in place (e.g. timeout retune).

        Returns ``True`` when the entry was found and replaced,
        ``False`` otherwise. Used by future tooling that retroactively
        tightens timeouts or flips ``enabled``.
        """
        with self._lock:
            existing = self._servers.get(plugin_id)
            if existing is None:
                return False
            updated: list[MCPServerConfig] = []
            found = False
            for cfg in existing:
                if cfg.name == f"{plugin_id}__{server_name}":
                    updated.append(replace(new_cfg, name=cfg.name))
                    found = True
                else:
                    updated.append(cfg)
            if found:
                self._servers[plugin_id] = tuple(updated)
            return found


#: Process-global singleton consumed by :mod:`opencomputer.plugins.loader`
#: + :mod:`opencomputer.mcp.client`. Tests can construct fresh instances;
#: production hits this module-level handle.
default_registry: Final[BundleMcpRegistry] = BundleMcpRegistry()


__all__ = [
    "BundleMcpRegistry",
    "BundleMcpSafetyError",
    "PLUGIN_ROOT_PLACEHOLDER",
    "bundle_mcp_to_mcp_server_config",
    "default_registry",
    "expand_plugin_root_placeholder",
    "resolve_bundle_command",
]
