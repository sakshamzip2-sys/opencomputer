"""Downloads-cleanup MCP plugin (mcp-openclaw-port M5 reference).

Demonstrates the bundle-MCP authoring surface end-to-end. The plugin's
``register()`` is a no-op — the only side effect of activation is that
OC reads the manifest's ``bundle_mcp`` field and registers our local
``mcp_server.py`` in the global :class:`BundleMcpRegistry`. Tools
appear under the prefix ``downloads-cleanup-mcp__downloads__*``.

Spawn is lazy by default (manifest sets ``lazy: true``): the
subprocess only starts on first tool call, not at plugin activation.
This keeps ``oc chat`` cold-start time uncoupled from how many bundled
MCPs the user has installed.

This is a thin entry-point file; the real work is in
``mcp_server.py`` (the MCP server itself).
"""

from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.downloads_cleanup_mcp.plugin")


def register(api) -> None:  # noqa: ANN001 — PluginAPI duck-typed by contract
    """Plugin entry point — no runtime registrations needed.

    The bundle MCP lifecycle is handled by the loader:
    ``loader._register_bundle_mcps(candidate)`` picks up
    ``manifest.bundle_mcp`` and registers each entry on the
    :class:`opencomputer.mcp.bundle.BundleMcpRegistry`. We don't need
    to call any register-* method here.

    We do log at INFO so an operator running ``oc plugins`` can verify
    the plugin loaded cleanly.
    """
    _log.info(
        "downloads-cleanup-mcp registered — bundle MCP 'downloads' will "
        "spawn lazily on first tool call",
    )
