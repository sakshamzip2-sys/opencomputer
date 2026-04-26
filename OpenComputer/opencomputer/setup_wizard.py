"""
opencomputer setup — interactive first-run wizard.

Walks a new user through: pick provider → enter API key → optionally add
channel tokens → write config.yaml → test the provider connection.

Design notes:
- Never stores the API key in config.yaml — we ask the user to export it
  as an env var (the provider reads $ANTHROPIC_API_KEY / $OPENAI_API_KEY
  natively). Saves the ENV VAR NAME so we can remind the user later.
- Safe to re-run — each step detects existing config and asks "overwrite?"
- Provider test is short (just a <10-token ping) to confirm auth works.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

from opencomputer.agent.config import (
    Config,
    MCPServerConfig,
    ModelConfig,
    default_config,
)
from opencomputer.agent.config_store import (
    config_file_path,
    load_config,
    save_config,
)

console = Console()


# Round 2B P-10 — channel selection → required-plugin id mapping.
#
# The setup wizard's channel step only writes config.yaml; nothing in
# core wires channel selection to plugin activation. P-10 closes that
# loop by auto-enabling the plugin that backs each chosen channel so
# new users don't have to discover ``opencomputer plugin enable
# <id>`` separately.
#
# HARD CONSTRAINT (Phase 5.B identity): we ONLY enable plugins that
# are already discoverable on disk (bundled ``extensions/`` or
# ``~/.opencomputer/plugins/``). Nothing here downloads, fetches, or
# pip-installs anything. ``cli_plugin.plugin_enable`` already
# validates ids against ``discover()`` so unknown ids fail loud.
#
# Keys are user-facing channel names; values are the plugin ids those
# channels actually live under in ``extensions/``. The dict is
# intentionally permissive — any channel name not in the map is
# silently ignored (lets users type free-form names without crashing
# the wizard).
_CHANNEL_PLUGIN_MAP: dict[str, str] = {
    "telegram": "telegram",
    "discord": "discord",
    "slack": "slack",
    "matrix": "matrix",
    "mattermost": "mattermost",
    "imessage": "imessage",
    "signal": "signal",
    "whatsapp": "whatsapp",
    "webhook": "webhook",
    # Bundled directory is ``extensions/homeassistant/`` but operators
    # commonly type ``home-assistant`` (matches Home Assistant's own
    # branding) — accept both spellings, route to the same plugin id.
    "home-assistant": "homeassistant",
    "homeassistant": "homeassistant",
    "email": "email",
}


# Onboarding UX — channel platform registry (hermes parity).
#
# Mirrors hermes-agent's ``_GATEWAY_PLATFORMS`` at
# ``hermes_cli/setup.py:2210-2229``. Tuple shape:
# ``(label, primary_env_var, plugin_id)``. ``label`` is what the user
# sees in the surfaced list; ``primary_env_var`` is the credential we
# probe to detect "already configured" so the wizard can show
# ``[configured]`` next to those rows; ``plugin_id`` matches the
# bundled ``extensions/<id>/`` directory and is what
# ``_auto_enable_plugins_for_channels`` uses.
#
# Order is the order the user sees in the prompt — most-popular first.
_CHANNEL_PLATFORMS: list[tuple[str, str, str]] = [
    ("telegram", "TELEGRAM_BOT_TOKEN", "telegram"),
    ("discord", "DISCORD_BOT_TOKEN", "discord"),
    ("slack", "SLACK_BOT_TOKEN", "slack"),
    ("matrix", "MATRIX_ACCESS_TOKEN", "matrix"),
    ("mattermost", "MATTERMOST_TOKEN", "mattermost"),
    ("signal", "SIGNAL_HTTP_URL", "signal"),
    ("imessage", "IMESSAGE_DB_PATH", "imessage"),
    ("whatsapp", "WHATSAPP_ENABLED", "whatsapp"),
    ("webhook", "WEBHOOK_ENABLED", "webhook"),
    ("homeassistant", "HASS_TOKEN", "homeassistant"),
    ("email", "SMTP_HOST", "email"),
]


# Last-resort provider catalog when discovery fails or no plugin
# manifest declares ``setup.providers``. G.24 pushed this knowledge
# back into per-plugin manifests so third-party providers can
# self-describe; the dict only fires when discovery yields nothing.
_BUILTIN_PROVIDER_FALLBACK = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-opus-4-7",
        "signup_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "signup_url": "https://platform.openai.com/api-keys",
    },
}


def _discover_supported_providers() -> dict[str, dict]:
    """Build the provider catalog from plugin manifests (G.24).

    Walks every discoverable plugin candidate, reads its
    ``setup.providers`` declarations, and produces the dict the
    wizard expects. Plugin-declared values override the
    ``_BUILTIN_PROVIDER_FALLBACK`` entries; if the manifest omits a
    field (empty string), the fallback fills it in.

    Returns the legacy dict shape on any discovery failure so the
    wizard can never wedge on a bad filesystem.
    """
    catalog = {pid: dict(meta) for pid, meta in _BUILTIN_PROVIDER_FALLBACK.items()}
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths

        for cand in discover(standard_search_paths()):
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                entry = catalog.setdefault(prov.id, {})
                if prov.label:
                    entry["label"] = prov.label
                if prov.default_model:
                    entry["default_model"] = prov.default_model
                if prov.signup_url:
                    entry["signup_url"] = prov.signup_url
                if prov.env_vars:
                    # Manifest order is canonical — first env var wins.
                    entry["env_key"] = prov.env_vars[0]
                # Sensible defaults so the wizard never crashes on
                # partial declarations.
                entry.setdefault("label", prov.id.title())
                entry.setdefault("env_key", "")
                entry.setdefault("default_model", "")
                entry.setdefault("signup_url", "")
    except Exception:  # noqa: BLE001
        # Discovery failed — fall back to whatever the dict already has.
        pass
    # Drop entries that have no env_key — they can't be set up
    # interactively, so omitting them keeps the menu clean.
    return {pid: meta for pid, meta in catalog.items() if meta.get("env_key")}


# Built lazily so test fixtures that patch discovery still work.
def _get_supported_providers() -> dict[str, dict]:
    return _discover_supported_providers()


def _print_banner() -> None:
    console.print("\n[bold cyan]╭─────────────────────────────────────╮[/bold cyan]")
    console.print("[bold cyan]│    OpenComputer — Setup Wizard      │[/bold cyan]")
    console.print("[bold cyan]╰─────────────────────────────────────╯[/bold cyan]")
    console.print()


def _pick_provider() -> tuple[str, dict]:
    providers = _get_supported_providers()
    console.print("[bold]Step 1 — pick an LLM provider[/bold]")
    for i, (pid, meta) in enumerate(providers.items(), 1):
        console.print(f"  [cyan]{i}[/cyan]. {meta['label']} — [dim]{pid}[/dim]")
    while True:
        choice = Prompt.ask(
            "Choose",
            default="1",
            choices=[str(i) for i in range(1, len(providers) + 1)],
        )
        try:
            idx = int(choice) - 1
        except ValueError:
            continue
        pid = list(providers.keys())[idx]
        return pid, providers[pid]


def _prompt_model(default_model: str) -> str:
    console.print(f"\n[bold]Step 2 — which model?[/bold] [dim](default: {default_model})[/dim]")
    return Prompt.ask("Model", default=default_model)


def _prompt_api_key(env_key: str, signup_url: str) -> None:
    console.print("\n[bold]Step 3 — API key[/bold]")
    console.print(f"[dim]Get one at {signup_url} if you don't have it yet.[/dim]")

    current = os.environ.get(env_key, "")
    if current:
        console.print(
            f"[green]✓[/green] {env_key} is already set in your environment "
            f"(ends in …{current[-4:]})."
        )
        return

    console.print(
        f"[yellow]![/yellow] {env_key} is NOT set. Before running, export it in your shell:"
    )
    console.print(f"  [bold]export {env_key}=your-key-here[/bold]")
    console.print("[dim]Tip: add it to ~/.zshrc or ~/.bashrc to persist across sessions.[/dim]")


def _required_plugins_for_channels(channels: list[str]) -> set[str]:
    """Map channel names → plugin ids needed to back them (Round 2B P-10).

    Channel names that don't appear in ``_CHANNEL_PLUGIN_MAP`` are
    silently dropped — channel selection is free-form input, so a
    typo or future channel id mustn't crash the wizard. Returns a
    plain ``set`` so callers can do trivial ``- enabled`` math.
    """
    out: set[str] = set()
    for name in channels:
        plugin_id = _CHANNEL_PLUGIN_MAP.get(name)
        if plugin_id is not None:
            out.add(plugin_id)
    return out


def _currently_enabled_plugin_ids() -> set[str]:
    """Read the active profile's ``plugins.enabled`` list (P-10 helper).

    Returns the set of plugin ids currently enabled for the active
    profile. Empty set when ``profile.yaml`` is absent, malformed, or
    declares the wildcard ``"*"`` (which means "all discovered
    plugins" — the auto-enable prompt is then a no-op because the
    needed channel plugin is already loaded). Never raises — config
    issues fall through to "nothing enabled" so the wizard can still
    offer to enable.
    """
    try:
        from opencomputer.cli_plugin import _active_profile_yaml_path

        path, _profile_name = _active_profile_yaml_path()
        if not path.exists():
            return set()
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001
        return set()
    if not isinstance(raw, dict):
        return set()
    plugins_block = raw.get("plugins")
    if not isinstance(plugins_block, dict):
        return set()
    enabled = plugins_block.get("enabled")
    if enabled == "*":
        # Wildcard means everything is loaded — treat as "all needed
        # plugins are already on" so the auto-enable prompt no-ops.
        return set(_CHANNEL_PLUGIN_MAP.values())
    if not isinstance(enabled, list):
        return set()
    return {pid for pid in enabled if isinstance(pid, str)}


def _auto_enable_plugins_for_channels(channels: list[str]) -> None:
    """Prompt user to enable plugins required by their selected channels.

    Round 2B P-10. Compares the channels the user opted into against
    the active profile's enabled plugins and offers a single combined
    confirmation to enable any that are missing. Honours the hard
    constraint: only plugins already discoverable via
    ``cli_plugin.plugin_enable`` (bundled ``extensions/`` or
    ``~/.opencomputer/plugins/``) get touched — nothing is downloaded,
    pip-installed, or fetched from a remote registry. Errors raised
    by ``plugin_enable`` (typer.Exit codes for unknown ids, etc.)
    surface as a yellow warning rather than crashing the wizard.
    """
    needed = _required_plugins_for_channels(channels)
    if not needed:
        return
    missing = needed - _currently_enabled_plugin_ids()
    if not missing:
        return

    from opencomputer import cli_plugin

    pretty = ", ".join(sorted(missing))
    console.print(
        f"\n[dim]These channels need plugins that aren't yet enabled: "
        f"{pretty}[/dim]"
    )
    if not Confirm.ask(
        f"Enable required channel plugins ({pretty})?",
        default=True,
    ):
        return
    for pid in sorted(missing):
        try:
            cli_plugin.plugin_enable(pid)
        except SystemExit:
            # typer.Exit subclasses SystemExit. plugin_enable raises
            # exit-0 on "already enabled" no-op (benign) and exit-1
            # on unknown ids (worth surfacing). Either way, swallow
            # so one missing plugin doesn't kill the wizard.
            continue
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]![/yellow] could not enable '{pid}': "
                f"{type(exc).__name__}: {exc}"
            )


def _optional_channel(cfg: Config) -> None:
    """Walk the user through enabling messaging channel plugins.

    Hermes parity (``hermes_cli/setup.py:2232-2256``): show every
    platform in :data:`_CHANNEL_PLATFORMS` with a ``[configured]`` mark
    next to the ones whose primary env var is already set, then accept
    space-separated channel ids. Unknown ids are silently dropped so
    typos don't crash the wizard.
    """
    console.print("\n[bold]Step 4 — messaging channels (optional)[/bold]")
    console.print(
        "[dim]Select any channels you want enabled. "
        "Skip if you only want the CLI for now.[/dim]\n"
    )

    for label, env_var, _plugin_id in _CHANNEL_PLATFORMS:
        is_set = bool(os.environ.get(env_var))
        suffix = "  [green][configured][/green]" if is_set else ""
        console.print(f"  [cyan]{label}[/cyan]  [dim]({env_var})[/dim]{suffix}")

    # Default is intentionally empty — ``[configured]`` is informational
    # only. Pre-filling with already-configured channels would silently
    # re-run ``_auto_enable_plugins_for_channels`` on every Enter,
    # which is surprising for returning users in Quick mode who just
    # want to skip the step.
    raw = Prompt.ask(
        "\nChannels to enable (space-separated, blank to skip)",
        default="",
    )
    selected = [c.strip().lower() for c in raw.split() if c.strip()]
    selected = [c for c in selected if c in _CHANNEL_PLUGIN_MAP]
    if not selected:
        return

    _auto_enable_plugins_for_channels(selected)


def _optional_mcp(cfg: Config) -> Config:
    console.print("\n[bold]Step 5 — MCP servers (optional)[/bold]")
    console.print(
        "[dim]MCP servers expose external tools to the agent "
        "(stock prices, databases, browsers, etc.)[/dim]"
    )
    want_mcp = Confirm.ask("Add an MCP server?", default=False)
    if not want_mcp:
        return cfg

    servers: list[MCPServerConfig] = list(cfg.mcp.servers)
    while True:
        name = Prompt.ask("Server name (kebab-case, e.g. 'investor-agent')")
        if not name.strip():
            break
        command = Prompt.ask("Command to launch it (e.g. 'python3')")
        args_str = Prompt.ask("Args (space-separated, e.g. '-m investor_agent.server')", default="")
        args = tuple(args_str.split()) if args_str else ()
        servers.append(MCPServerConfig(name=name, command=command, args=args, enabled=True))
        console.print(f"[green]✓[/green] added {name}")
        if not Confirm.ask("Add another?", default=False):
            break

    return replace(cfg, mcp=replace(cfg.mcp, servers=tuple(servers)))


async def _test_provider(provider_id: str, env_key: str) -> bool:
    """Fire one tiny request to confirm auth works. Returns True on success."""
    if not os.environ.get(env_key):
        console.print(f"[yellow]skipped[/yellow] — {env_key} not set, can't test auth yet")
        return False

    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry
    from plugin_sdk.core import Message

    # Discover + activate providers
    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        plugin_registry.load_all([ext_dir])

    provider_cls = plugin_registry.providers.get(provider_id)
    if provider_cls is None:
        console.print(f"[red]✗[/red] provider plugin for '{provider_id}' not found")
        return False

    try:
        provider = provider_cls() if isinstance(provider_cls, type) else provider_cls
        resp = await provider.complete(
            model=default_config().model.model if provider_id == "anthropic" else "gpt-5.4",
            messages=[Message(role="user", content="reply with exactly: OK")],
            max_tokens=8,
        )
        console.print(
            f"[green]✓[/green] provider responded — "
            f"{resp.usage.input_tokens} in / {resp.usage.output_tokens} out tokens"
        )
        return True
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗[/red] provider test failed: {type(e).__name__}: {e}")
        return False


def run_setup() -> None:
    """Interactive setup wizard entry point.

    Hermes parity:

    - TTY guard up-front (``hermes_cli/main.py::_require_tty``) — we
      refuse with a clear stderr message when stdin is a pipe so a
      ``opencomputer setup < something.txt`` invocation doesn't hang.
    - On existing config, returning users see a Welcome Back menu
      (Quick / Full / individual section / Exit) instead of the
      destructive Overwrite? Y/N. Mirrors hermes' menu at
      ``hermes_cli/setup.py:2982-3018``.
    """
    from opencomputer.cli import _require_tty

    _require_tty("setup")

    _print_banner()

    if config_file_path().exists():
        return _setup_returning_user()
    _run_full_setup(default_config())


def _setup_returning_user() -> None:
    console.print(
        f"[yellow]![/yellow] Existing config at [dim]{config_file_path()}[/dim]"
    )
    mode = Prompt.ask(
        "Quick (only fix what's missing), Full (reconfigure everything), "
        "or Exit?",
        choices=["quick", "full", "exit"],
        default="quick",
    )
    if mode == "exit":
        console.print("[dim]Aborted.[/dim]")
        return
    cfg = load_config()
    if mode == "full":
        _run_full_setup(cfg)
    else:
        _quick_setup(cfg)


def _run_full_setup(cfg: Config) -> None:
    provider_id, meta = _pick_provider()
    model = _prompt_model(meta["default_model"])
    cfg = replace(
        cfg,
        model=ModelConfig(
            provider=provider_id,
            model=model,
            api_key_env=meta["env_key"],
        ),
    )

    _prompt_api_key(meta["env_key"], meta["signup_url"])
    _optional_channel(cfg)
    cfg = _optional_mcp(cfg)

    save_config(cfg)
    console.print(
        f"\n[green]✓[/green] wrote config → [dim]{config_file_path()}[/dim]"
    )

    _optional_honcho()

    console.print("\n[bold]Step 6 — test the provider connection[/bold]")
    if Confirm.ask("Send a tiny test request now?", default=True):
        asyncio.run(_test_provider(provider_id, meta["env_key"]))

    console.print(
        "\n[bold green]Setup complete.[/bold green] "
        "Run [bold]opencomputer[/bold] to chat."
    )


def _quick_setup(cfg: Config) -> None:
    """Only re-prompt for items that are still missing.

    Mirrors hermes' ``_run_quick_setup`` at ``hermes_cli/setup.py:3156``
    — checks the provider's env var first (the only "required" item),
    then offers to add channels / MCP servers as opt-in. No destructive
    overwrite of any setting the user already has.
    """
    provider_id = cfg.model.provider
    env_key = cfg.model.api_key_env or _get_supported_providers().get(
        provider_id, {}
    ).get("env_key", "")
    signup_url = (
        _get_supported_providers().get(provider_id, {}).get("signup_url", "")
    )

    actions_taken = 0

    if env_key and not os.environ.get(env_key):
        _prompt_api_key(env_key, signup_url)
        actions_taken += 1

    if Confirm.ask("Add or change channels?", default=False):
        _optional_channel(cfg)
        actions_taken += 1

    if Confirm.ask("Add or change MCP servers?", default=False):
        cfg = _optional_mcp(cfg)
        save_config(cfg)
        actions_taken += 1

    if actions_taken == 0:
        console.print(
            "[dim]Nothing to fix. "
            "Run setup again and pick `full` to reconfigure from scratch.[/dim]"
        )
    else:
        console.print(
            f"\n[bold green]Quick setup done.[/bold green] "
            f"{actions_taken} item(s) updated."
        )


def _load_honcho_bootstrap():
    """Thin re-export of ``cli_memory._load_honcho_bootstrap`` so tests can
    monkeypatch it on the wizard module without reaching across imports.

    Returns the bootstrap module or ``None`` if the plugin isn't present.
    """
    try:
        from opencomputer.cli_memory import _load_honcho_bootstrap as _loader
    except Exception:
        return None
    try:
        return _loader()
    except Exception:
        return None


def _downgrade_memory_provider_to_empty() -> None:
    """Persist ``memory.provider=""`` to the on-disk config.

    Called when Docker is absent or when ``ensure_started()`` fails —
    next wizard/CLI invocation should NOT retry the Honcho bring-up
    until the user explicitly runs ``opencomputer memory setup``.
    """
    try:
        cfg = load_config()
        new_cfg = replace(cfg, memory=replace(cfg.memory, provider=""))
        save_config(new_cfg)
    except Exception as exc:  # noqa: BLE001
        # Never crash the wizard on a config-write failure — just report.
        console.print(
            f"[yellow]![/yellow] Could not update config to record baseline "
            f"memory preference: {type(exc).__name__}: {exc}"
        )


def _optional_honcho() -> None:
    """Phase 12b1 / A5 — silent Honcho activation when Docker is present,
    honest baseline notice when not.

    Contract (no user prompt — this function never calls ``Confirm.ask``):

    * Docker + compose v2 detected → call ``bootstrap.ensure_started``
      (the A3 idempotent helper with port-collision detection + pull +
      health poll). On success: print "Honcho memory running" banner and
      leave ``memory.provider="memory-honcho"`` as-is (A4 default). On
      failure: print the error, persist ``provider=""`` so subsequent
      runs don't retry until the user fixes it.
    * Docker absent (or compose v2 missing) → print a non-alarming
      notice pointing at the install URL and persist ``provider=""``.
    """
    bootstrap = _load_honcho_bootstrap()
    if bootstrap is None:
        # Plugin genuinely absent from this install — fall through the
        # same path as "no Docker": turn off retries and move on.
        console.print(
            "[yellow]ℹ[/yellow] memory-honcho plugin not present — "
            "running on baseline memory."
        )
        _downgrade_memory_provider_to_empty()
        return

    docker, compose_v2 = bootstrap.detect_docker()
    if not docker or not compose_v2:
        console.print(
            "[yellow]ℹ[/yellow] Running on baseline memory. "
            "Install Docker to enable advanced memory features: "
            "https://docs.docker.com/get-docker/"
        )
        _downgrade_memory_provider_to_empty()
        return

    console.print("[dim]Starting Honcho memory stack…[/dim]")
    try:
        ok, msg = bootstrap.ensure_started(timeout_s=60)
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"{type(exc).__name__}: {exc}"

    if ok:
        console.print(
            "[green]✓[/green] Honcho memory running on http://localhost:8000"
        )
        return

    console.print(f"[red]✗[/red] {msg}")
    console.print(
        "[dim]Continuing on baseline memory. Fix the issue and re-run "
        "[cyan]opencomputer memory setup[/cyan] to enable Honcho later.[/dim]"
    )
    _downgrade_memory_provider_to_empty()


__all__ = [
    "_auto_enable_plugins_for_channels",
    "_required_plugins_for_channels",
    "run_setup",
]
