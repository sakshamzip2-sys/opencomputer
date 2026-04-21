"""
opencomputer doctor — diagnose common issues.

Runs a battery of checks and prints a pass/fail report. Intended to be
the first thing a user runs when something isn't working.

Checks:
  1. Python version
  2. Config file exists + is valid YAML
  3. Configured provider's plugin is installed
  4. Provider API key is set in environment
  5. Optional channel tokens are set if configured
  6. Session DB is writable
  7. Skills directory is writable
  8. MCP servers can be reached (skipped if none configured)
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

console = Console()


@dataclass(slots=True)
class Check:
    name: str
    status: Literal["pass", "fail", "warn", "skip"]
    detail: str = ""


def _status_icon(s: str) -> str:
    return {"pass": "[green]✓[/green]", "fail": "[red]✗[/red]",
            "warn": "[yellow]![/yellow]", "skip": "[dim]·[/dim]"}[s]


def _check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) < (3, 12):
        return Check(
            "python version", "fail",
            f"need Python >=3.12, got {v.major}.{v.minor}.{v.micro}"
        )
    return Check("python version", "pass", f"{v.major}.{v.minor}.{v.micro}")


def _check_config() -> tuple[Check, object]:
    from opencomputer.agent.config_store import config_file_path, load_config

    path = config_file_path()
    if not path.exists():
        return (
            Check(
                "config file", "warn",
                f"no config at {path} — run `opencomputer setup`"
            ),
            None,
        )
    try:
        cfg = load_config()
        return Check("config file", "pass", str(path)), cfg
    except Exception as e:  # noqa: BLE001
        return Check("config file", "fail", f"{path}: {e}"), None


def _check_provider_plugin(cfg) -> Check:
    from opencomputer.plugins.registry import registry as plugin_registry

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        plugin_registry.load_all([ext_dir])

    if cfg is None:
        return Check("provider plugin", "skip", "no config")

    provider_id = cfg.model.provider
    if provider_id in plugin_registry.providers:
        return Check("provider plugin", "pass", f"'{provider_id}' registered")
    return Check(
        "provider plugin", "fail",
        f"provider '{provider_id}' not found. "
        f"installed: {list(plugin_registry.providers.keys()) or 'none'}"
    )


def _check_provider_key(cfg) -> Check:
    if cfg is None:
        return Check("provider API key", "skip", "no config")
    env_key = cfg.model.api_key_env
    if os.environ.get(env_key):
        return Check("provider API key", "pass", f"{env_key} is set")
    return Check(
        "provider API key", "fail",
        f"{env_key} not set — export it before running"
    )


def _check_session_db(cfg) -> Check:
    if cfg is None:
        return Check("session DB", "skip", "no config")
    db_path = cfg.session.db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        test_file = db_path.parent / ".writetest"
        test_file.write_text("x")
        test_file.unlink()
        return Check("session DB path", "pass", str(db_path))
    except Exception as e:  # noqa: BLE001
        return Check("session DB path", "fail", f"{db_path}: {e}")


def _check_skills_dir(cfg) -> Check:
    if cfg is None:
        return Check("skills dir", "skip", "no config")
    p = cfg.memory.skills_path
    try:
        p.mkdir(parents=True, exist_ok=True)
        return Check("skills dir", "pass", str(p))
    except Exception as e:  # noqa: BLE001
        return Check("skills dir", "fail", f"{p}: {e}")


def _check_channel_tokens(cfg) -> list[Check]:
    out: list[Check] = []
    if cfg is None:
        return out
    from opencomputer.plugins.registry import registry as plugin_registry

    # Generic: for each channel plugin registered, check its conventional env var.
    # We know about Telegram specifically because it's bundled.
    if "telegram" in plugin_registry.channels:
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tok:
            out.append(Check("telegram token", "pass", "TELEGRAM_BOT_TOKEN set"))
        else:
            out.append(Check("telegram token", "skip", "TELEGRAM_BOT_TOKEN not set"))
    return out


async def _check_mcp(cfg) -> list[Check]:
    out: list[Check] = []
    if cfg is None or not cfg.mcp.servers:
        return out

    from opencomputer.mcp.client import MCPConnection

    for server in cfg.mcp.servers:
        if not server.enabled:
            out.append(Check(f"mcp:{server.name}", "skip", "disabled in config"))
            continue
        conn = MCPConnection(config=server)
        try:
            ok = await conn.connect()
            if ok:
                out.append(
                    Check(f"mcp:{server.name}", "pass", f"{len(conn.tools)} tool(s)")
                )
            else:
                out.append(Check(f"mcp:{server.name}", "fail", "connect returned False"))
        finally:
            await conn.disconnect()
    return out


def run_doctor() -> int:
    """Run all checks and print a report. Returns the number of failed checks."""
    console.print("\n[bold cyan]OpenComputer — Doctor[/bold cyan]\n")

    checks: list[Check] = [_check_python()]
    cfg_check, cfg = _check_config()
    checks.append(cfg_check)

    checks.append(_check_provider_plugin(cfg))
    checks.append(_check_provider_key(cfg))
    checks.append(_check_session_db(cfg))
    checks.append(_check_skills_dir(cfg))
    checks.extend(_check_channel_tokens(cfg))

    try:
        mcp_checks = asyncio.run(_check_mcp(cfg))
    except RuntimeError:
        mcp_checks = []
    checks.extend(mcp_checks)

    # Print
    max_name = max(len(c.name) for c in checks)
    for c in checks:
        pad = " " * (max_name - len(c.name))
        detail = f"  [dim]— {c.detail}[/dim]" if c.detail else ""
        console.print(f"  {_status_icon(c.status)}  {c.name}{pad}{detail}")

    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    console.print()
    if failures:
        console.print(
            f"[red bold]{failures} failure(s)[/red bold] — fix these before running."
        )
    elif warnings:
        console.print(
            f"[yellow bold]{warnings} warning(s)[/yellow bold] — should still work."
        )
    else:
        console.print("[green bold]All checks passed.[/green bold]")
    return failures


__all__ = ["run_doctor"]
