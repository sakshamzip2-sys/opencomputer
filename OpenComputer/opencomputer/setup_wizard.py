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


# Known providers the wizard supports out of the box. Adding a new provider
# plugin? It'll still work — the user can edit config.yaml by hand.
_SUPPORTED_PROVIDERS = {
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


def _print_banner() -> None:
    console.print("\n[bold cyan]╭─────────────────────────────────────╮[/bold cyan]")
    console.print("[bold cyan]│    OpenComputer — Setup Wizard      │[/bold cyan]")
    console.print("[bold cyan]╰─────────────────────────────────────╯[/bold cyan]")
    console.print()


def _pick_provider() -> tuple[str, dict]:
    console.print("[bold]Step 1 — pick an LLM provider[/bold]")
    for i, (pid, meta) in enumerate(_SUPPORTED_PROVIDERS.items(), 1):
        console.print(f"  [cyan]{i}[/cyan]. {meta['label']} — [dim]{pid}[/dim]")
    while True:
        choice = Prompt.ask(
            "Choose", default="1", choices=[str(i) for i in range(1, len(_SUPPORTED_PROVIDERS) + 1)]
        )
        try:
            idx = int(choice) - 1
        except ValueError:
            continue
        pid = list(_SUPPORTED_PROVIDERS.keys())[idx]
        return pid, _SUPPORTED_PROVIDERS[pid]


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
        f"[yellow]![/yellow] {env_key} is NOT set. "
        f"Before running, export it in your shell:"
    )
    console.print(f"  [bold]export {env_key}=your-key-here[/bold]")
    console.print(
        "[dim]Tip: add it to ~/.zshrc or ~/.bashrc to persist across sessions.[/dim]"
    )


def _optional_channel(cfg: Config) -> None:
    console.print("\n[bold]Step 4 — messaging channel (optional)[/bold]")
    console.print("[dim]Skip if you only want to use the CLI for now.[/dim]")

    want_telegram = Confirm.ask("Set up Telegram?", default=False)
    if want_telegram:
        console.print(
            "1. Open Telegram → message @BotFather → /newbot\n"
            "2. Name the bot, get the token.\n"
            "3. Export the token:"
        )
        console.print("   [bold]export TELEGRAM_BOT_TOKEN=123:ABC...[/bold]")
        console.print(
            "[dim]Then run `opencomputer gateway` — the Telegram plugin "
            "picks up the token automatically.[/dim]"
        )


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
        args_str = Prompt.ask(
            "Args (space-separated, e.g. '-m investor_agent.server')", default=""
        )
        args = tuple(args_str.split()) if args_str else ()
        servers.append(
            MCPServerConfig(name=name, command=command, args=args, enabled=True)
        )
        console.print(f"[green]✓[/green] added {name}")
        if not Confirm.ask("Add another?", default=False):
            break

    return replace(cfg, mcp=replace(cfg.mcp, servers=tuple(servers)))


async def _test_provider(provider_id: str, env_key: str) -> bool:
    """Fire one tiny request to confirm auth works. Returns True on success."""
    if not os.environ.get(env_key):
        console.print(
            f"[yellow]skipped[/yellow] — {env_key} not set, can't test auth yet"
        )
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
    """Interactive setup wizard entry point."""
    _print_banner()

    existing = config_file_path().exists()
    if existing:
        console.print(
            f"[yellow]![/yellow] Existing config found at [dim]{config_file_path()}[/dim]"
        )
        if not Confirm.ask("Overwrite?", default=False):
            console.print("[dim]Aborted.[/dim]")
            return

    cfg = load_config() if existing else default_config()
    provider_id, meta = _pick_provider()
    model = _prompt_model(meta["default_model"])
    new_model_cfg = ModelConfig(
        provider=provider_id,
        model=model,
        api_key_env=meta["env_key"],
    )
    cfg = replace(cfg, model=new_model_cfg)

    _prompt_api_key(meta["env_key"], meta["signup_url"])
    _optional_channel(cfg)
    cfg = _optional_mcp(cfg)

    save_config(cfg)
    console.print(f"\n[green]✓[/green] wrote config → [dim]{config_file_path()}[/dim]")

    console.print("\n[bold]Step 6 — test the provider connection[/bold]")
    if Confirm.ask("Send a tiny test request now?", default=True):
        asyncio.run(_test_provider(provider_id, meta["env_key"]))

    console.print(
        "\n[bold green]Setup complete.[/bold green] Run [bold]opencomputer[/bold] to chat."
    )


__all__ = ["run_setup"]
