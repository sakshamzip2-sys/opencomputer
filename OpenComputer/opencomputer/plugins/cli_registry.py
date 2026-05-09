"""v1.1 plan-4 M13 — plugin-authored top-level CLI command registration.

This module owns:

* ``CORE_RESERVED_CLI_NAMES`` — the set of root verbs core ships, against
  which every plugin-registered ``oc <name>`` must collide-check (always
  fatal, irrespective of ``replace=``).
* ``register_plugin_cli_commands(typer_app)`` — called from ``cli.py``
  after the root Typer ``app`` is fully built. Reads the cheap manifest
  scan; for each manifest's ``cli_commands`` entry, attaches a lazy
  placeholder Typer that loads the owning plugin on actual invocation
  and dispatches into the plugin's registered command app.

The discovery pass DOES NOT import any plugin's Python — only manifest
JSON. The plugin's ``register(api)`` runs the first time the user types
``oc <name> ...`` and we resolve the placeholder.
"""

from __future__ import annotations

import logging

import typer

# Re-export at module level so tests can monkeypatch the surface this
# module actually consumes (Karpathy: surgical-changes — keep the seam
# at the place where the dependency enters this module).
from opencomputer.plugins.discovery import discover, standard_search_paths
from opencomputer.profiles import read_active_profile

logger = logging.getLogger(__name__)


# Reserved by core. A plugin trying to register one of these is rejected
# with PluginCLINameCollision — these verbs are core's surface and must
# stay unambiguous (no plugin can shadow `oc chat`).
#
# Sources:
# * `@app.command()` decorators in opencomputer/cli.py (chat, code,
#   resume, search, sessions, wire, plugins, setup, doctor, skills,
#   recall, steer, batch, update; named: oneshot, model, login, logout).
# * `app.add_typer(...)` sub-apps (gateway, pairing, policy, agents,
#   config, mcp, eval, browser, memory, honcho, auth, help, backup,
#   hooks, preset, profile, bindings, plugin, channels, adapter,
#   consent, dashboard, tui, checkpoints, worktrees, rules, service,
#   cost, optimize, langfuse, cron, heartbeat, pair).
# * Roadmap names reserved for Tier-1 work (usage, insights, audit,
#   session, routing) so a plugin doesn't squat them before core ships.
CORE_RESERVED_CLI_NAMES: frozenset[str] = frozenset(
    {
        # Root commands
        "chat",
        "code",
        "resume",
        "search",
        "sessions",
        "wire",
        "plugins",
        "setup",
        "doctor",
        "skills",
        "recall",
        "steer",
        "batch",
        "update",
        "oneshot",
        "model",
        "login",
        "logout",
        # Sub-app names
        "gateway",
        "pairing",
        "policy",
        "agents",
        "config",
        "mcp",
        "eval",
        "browser",
        "memory",
        "honcho",
        "auth",
        "help",
        "backup",
        "hooks",
        "preset",
        "profile",
        "bindings",
        "plugin",
        "channels",
        "adapter",
        "consent",
        "dashboard",
        "tui",
        "checkpoints",
        "worktrees",
        "rules",
        "service",
        "cost",
        "optimize",
        "langfuse",
        "cron",
        "heartbeat",
        "pair",
        # Reserved for roadmap (Tier-1 work in plans 3-6)
        "usage",
        "insights",
        "audit",
        "session",
        "routing",
        "ui",  # tui already reserved; keep ui safe too
        "voice",  # voice plugin shipped via extensions; keep core surface
        "files",  # SP3 files-API CLI
    }
)


# ─── lazy-load placeholder ────────────────────────────────────────────────


def _attach_lazy_command(
    typer_app: typer.Typer,
    plugin_id: str,
    plugin_name: str,
    command_name: str,
) -> None:
    """Attach a lazy ``oc <command_name>`` command on ``typer_app``.

    Implemented as a single ``@typer_app.command()`` (not a sub-Typer)
    because top-level ``add_typer`` placeholders fail to materialize when
    they have no nested commands at scan time. The single command
    accepts arbitrary trailing args/options and forwards them to the
    plugin's real Typer once the plugin is loaded.

    On invocation we:

    1. Load the named plugin via ``opencomputer.plugins.registry`` —
       this fires its ``register(api)`` so the real Typer is captured.
    2. Pull the captured Typer from the shared
       ``api._cli_commands[command_name]`` map.
    3. Re-invoke that Typer with the recovered argv.
    """

    @typer_app.command(
        name=command_name,
        help=(
            f"Lazy-loaded subcommand provided by plugin {plugin_id!r} "
            f"({plugin_name}). Run with --help to see actual options "
            "after the plugin loads."
        ),
        # `--help` on the lazy command shows the placeholder help (it
        # reveals the plugin id + name). To get the real app's help the
        # user types `oc <name> <subcommand> --help`. Keeping --help here
        # also avoids typer's single-command-mode treating `--help` as a
        # bare arg when root has only one registered command (test
        # ergonomics + production safety).
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        },
    )
    def _lazy_dispatch(ctx: typer.Context) -> None:
        # Keep these imports module-relative so monkeypatch at the
        # module level (cli_registry.discover, cli_registry.load_plugin)
        # actually reaches the lazy code path during tests.
        from opencomputer.plugins import cli_registry as _self
        from opencomputer.plugins import loader as _loader
        from opencomputer.plugins.registry import registry

        api = registry.shared_api or registry.api()
        if registry.shared_api is None:
            registry.shared_api = api
        # Activate the owning plugin if we haven't already.
        already = command_name in api._cli_commands
        if not already:
            for cand in _self.discover(_self.standard_search_paths()):
                if cand.manifest.id == plugin_id:
                    _loader.load_plugin(cand, api)
                    break
        real_app = api._cli_commands.get(command_name)
        if real_app is None:
            typer.echo(
                f"Error: plugin {plugin_id!r} declared `cli_commands: "
                f"[\"{command_name}\"]` in its manifest but did not call "
                "api.register_cli_command() during register(api). Fix "
                "the plugin or remove the manifest declaration.",
                err=True,
            )
            raise typer.Exit(code=2)
        argv = list(ctx.args)
        try:
            real_app(args=argv, prog_name=command_name, standalone_mode=False)
        except SystemExit as e:  # typer/click exits via SystemExit
            raise typer.Exit(code=int(e.code) if isinstance(e.code, int) else 1)


# ─── top-level loader called from cli.py ─────────────────────────────────


def register_plugin_cli_commands(typer_app: typer.Typer) -> None:
    """Attach all plugin-advertised CLI commands as lazy placeholders.

    Called once at CLI startup — discovery is cheap (no plugin imports);
    each placeholder loads its plugin only when actually invoked.

    Profile-scoped (``cli_commands_profiles``) commands are filtered
    against the active profile here so they don't even appear in
    ``oc --help`` under a non-matching profile.

    Skips silently if Typer can't accept the add_typer call (e.g. the
    name is now in conflict with a core sub-app added in the same
    process — shouldn't happen with CORE_RESERVED_CLI_NAMES validation
    on the registration side, but stays defensive).
    """
    try:
        active_profile = read_active_profile() or "default"
        candidates = discover(standard_search_paths())
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "M13: skipping plugin CLI registration — manifest discovery failed: %s",
            e,
        )
        return

    seen: set[str] = set()
    for cand in candidates:
        for name in cand.manifest.cli_commands:
            if name in CORE_RESERVED_CLI_NAMES:
                logger.warning(
                    "M13: plugin %r advertises CLI command %r which is a "
                    "reserved core verb; ignored. Rename via the "
                    "`<plugin-id>-<verb>` convention.",
                    cand.manifest.id,
                    name,
                )
                continue
            if name in seen:
                logger.warning(
                    "M13: plugin %r advertises CLI command %r already "
                    "advertised by a previous plugin; ignored.",
                    cand.manifest.id,
                    name,
                )
                continue
            scopes = cand.manifest.cli_commands_profiles
            if scopes and "*" not in scopes and active_profile not in scopes:
                continue
            try:
                _attach_lazy_command(
                    typer_app, cand.manifest.id, cand.manifest.name, name
                )
                seen.add(name)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "M13: failed to attach plugin %r CLI command %r: %s",
                    cand.manifest.id,
                    name,
                    e,
                )


__all__ = ["CORE_RESERVED_CLI_NAMES", "register_plugin_cli_commands"]
