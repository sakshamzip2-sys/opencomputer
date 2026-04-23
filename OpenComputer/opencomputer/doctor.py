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
    return {
        "pass": "[green]✓[/green]",
        "fail": "[red]✗[/red]",
        "warn": "[yellow]![/yellow]",
        "skip": "[dim]·[/dim]",
    }[s]


def _check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) < (3, 12):
        return Check(
            "python version", "fail", f"need Python >=3.12, got {v.major}.{v.minor}.{v.micro}"
        )
    return Check("python version", "pass", f"{v.major}.{v.minor}.{v.micro}")


def _check_config() -> tuple[Check, object]:
    from opencomputer.agent.config_store import config_file_path, load_config

    path = config_file_path()
    if not path.exists():
        return (
            Check("config file", "warn", f"no config at {path} — run `opencomputer setup`"),
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
        "provider plugin",
        "fail",
        f"provider '{provider_id}' not found. "
        f"installed: {list(plugin_registry.providers.keys()) or 'none'}",
    )


def _check_provider_key(cfg) -> Check:
    if cfg is None:
        return Check("provider API key", "skip", "no config")
    env_key = cfg.model.api_key_env
    if os.environ.get(env_key):
        return Check("provider API key", "pass", f"{env_key} is set")
    return Check("provider API key", "fail", f"{env_key} not set — export it before running")


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


def _check_profile_artifacts() -> list[Check]:
    """Phase 14.F / C4 — validate per-profile C1/C2/C3 artifacts exist.

    Runs for the sticky active profile only. Default profile
    (no sticky file / sticky="default") produces skip checks — the
    default profile doesn't have a ``home/`` subdir or wrapper by
    design (it uses the user's real HOME and `opencomputer` binary).

    Each check is WARN on miss (not fail): a user may have intentionally
    deleted a wrapper or SOUL.md. The point is to surface drift, not
    block startup.
    """
    import sys as _sys

    from opencomputer.profiles import (
        ProfileNameError,
        get_profile_dir,
        read_active_profile,
        wrapper_path,
    )

    out: list[Check] = []

    try:
        active = read_active_profile()
    except Exception as e:  # noqa: BLE001 — never let doctor crash
        return [Check("profile artifacts", "fail", f"could not read active profile: {e}")]

    if active is None:
        # Default profile — no per-profile artifacts to validate.
        return [
            Check("profile home/", "skip", "default profile"),
            Check("profile wrapper", "skip", "default profile"),
            Check("profile SOUL.md", "skip", "default profile"),
        ]

    try:
        pdir = get_profile_dir(active)
    except ProfileNameError as e:
        return [Check("profile artifacts", "fail", f"sticky profile invalid: {e}")]

    # C1 — home/ subdir. Deliberately DO NOT call profile_home_dir (which
    # mkdirs on demand); we want to report drift, not silently repair.
    home_check_path = pdir / "home"
    if home_check_path.is_dir():
        out.append(Check("profile home/", "pass", str(home_check_path)))
    else:
        out.append(
            Check(
                "profile home/",
                "warn",
                f"expected {home_check_path} (use `opencomputer profile create` to re-seed)",
            )
        )

    # C2 — wrapper script (unix only)
    if _sys.platform.startswith("win") or os.name == "nt":
        out.append(Check("profile wrapper", "skip", "wrapper scripts unsupported on Windows"))
    else:
        wrapper = wrapper_path(active)
        if wrapper.exists():
            out.append(Check("profile wrapper", "pass", str(wrapper)))
        else:
            out.append(
                Check(
                    "profile wrapper",
                    "warn",
                    f"expected {wrapper} (use `opencomputer profile create` to re-seed)",
                )
            )

    # C3 — SOUL.md personality file
    soul = pdir / "SOUL.md"
    if soul.exists():
        out.append(Check("profile SOUL.md", "pass", str(soul)))
    else:
        out.append(
            Check(
                "profile SOUL.md",
                "warn",
                f"expected {soul} (use `opencomputer profile create` to re-seed)",
            )
        )

    return out


def _check_profile_and_overlay() -> list[Check]:
    """Phase 14.M/14.N doctor checks.

    M-check: profile.yaml references a preset that exists on disk.
    N-check: workspace overlay (if any) references a preset that exists
             and only names installed plugins in ``plugins.additional``.

    Both checks degrade gracefully — if there's no profile.yaml, no
    workspace overlay, or neither references anything to verify, the
    check is ``skip``. A malformed file -> ``fail`` (don't paper over).
    """
    from opencomputer.agent.config import _home
    from opencomputer.agent.profile_config import (
        ProfileConfigError,
        load_profile_config,
    )
    from opencomputer.agent.workspace import find_workspace_overlay
    from opencomputer.plugins.preset import list_presets, preset_path

    out: list[Check] = []

    # ── M-check: profile.yaml -> preset exists ─────────────────────────
    try:
        profile_cfg = load_profile_config(_home())
    except ProfileConfigError as e:
        out.append(
            Check(
                "profile.yaml",
                "fail",
                f"{_home() / 'profile.yaml'}: {e}",
            )
        )
        profile_cfg = None

    if profile_cfg is None:
        # Already reported the parse failure above.
        pass
    elif profile_cfg.preset is None:
        out.append(Check("profile preset", "skip", "no preset referenced"))
    else:
        target = preset_path(profile_cfg.preset)
        if target.exists():
            out.append(
                Check(
                    "profile preset",
                    "pass",
                    f"{profile_cfg.preset} -> {target}",
                )
            )
        else:
            available = list_presets() or ["(none)"]
            out.append(
                Check(
                    "profile preset",
                    "fail",
                    f"profile.yaml references preset "
                    f"{profile_cfg.preset!r} but {target} "
                    f"does not exist. available: {available}",
                )
            )

    # ── N-check: workspace overlay -> referents exist ─────────────────
    try:
        overlay = find_workspace_overlay()
    except ValueError as e:
        out.append(Check("workspace overlay", "fail", str(e)))
        return out

    if overlay is None:
        out.append(Check("workspace overlay", "skip", "no .opencomputer/ in CWD tree"))
        return out

    details: list[str] = []
    failed = False
    if overlay.preset is not None:
        tgt = preset_path(overlay.preset)
        if tgt.exists():
            details.append(f"preset={overlay.preset}")
        else:
            failed = True
            details.append(f"preset={overlay.preset!r} (MISSING: {tgt})")
    if overlay.plugins.additional:
        # We can only check *that* plugins are installed; the discovery
        # pass will produce a proper list later. For the doctor, a
        # shallow "id looks sane" check is enough — actual presence is
        # verified by the plugin loader.
        details.append(f"additional={list(overlay.plugins.additional)}")

    if failed:
        out.append(
            Check(
                "workspace overlay",
                "fail",
                f"{overlay.source_path}: " + "; ".join(details),
            )
        )
    else:
        out.append(
            Check(
                "workspace overlay",
                "pass",
                f"{overlay.source_path} — " + "; ".join(details or ["(empty)"]),
            )
        )

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
                out.append(Check(f"mcp:{server.name}", "pass", f"{len(conn.tools)} tool(s)"))
            else:
                out.append(Check(f"mcp:{server.name}", "fail", "connect returned False"))
        finally:
            await conn.disconnect()
    return out


async def _run_contributions(fix: bool) -> list[Check]:
    """Run all plugin-contributed health contributions. See plugin_sdk/doctor.py.

    Each contribution is `async (fix: bool) -> RepairResult`. When `fix=True`
    a contribution is expected to mutate state in place and set `repaired=True`
    on its result. Contributions raise-safe: any exception becomes a `fail`
    Check so one broken plugin can't crash the whole doctor run.

    Assumes plugins are already loaded — `_check_provider_plugin` runs earlier
    in `run_doctor` and does the `load_all` call. Reloading here would
    double-register tools (ValueError) and produce spurious failures.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    out: list[Check] = []
    for c in plugin_registry.doctor_contributions:
        try:
            result = await c.run(fix)
        except Exception as e:  # noqa: BLE001 — any plugin error becomes a fail
            out.append(Check(c.id, "fail", f"{type(e).__name__}: {e}"))
            continue
        detail = result.detail
        if result.repaired:
            detail = f"[repaired] {detail}" if detail else "[repaired]"
        out.append(Check(c.id, result.status, detail))
    return out


def run_doctor(fix: bool = False) -> int:
    """Run all checks and print a report. Returns the number of failed checks.

    When `fix=True`, every plugin-contributed HealthContribution is invoked
    with `fix=True`, giving it the chance to repair state in place rather
    than merely reporting the problem. Built-in checks are read-only either
    way; repair belongs to plugins (the core doesn't know which legacy
    config shape each plugin owns).
    """
    console.print("\n[bold cyan]OpenComputer — Doctor[/bold cyan]\n")
    if fix:
        console.print("[dim]--fix mode: contributions may repair state in place.[/dim]\n")

    checks: list[Check] = [_check_python()]
    cfg_check, cfg = _check_config()
    checks.append(cfg_check)

    checks.append(_check_provider_plugin(cfg))
    checks.append(_check_provider_key(cfg))
    checks.append(_check_session_db(cfg))
    checks.append(_check_skills_dir(cfg))
    checks.extend(_check_channel_tokens(cfg))
    checks.extend(_check_profile_and_overlay())
    # Phase 14.F / C4 — per-profile C1/C2/C3 artifact drift detection.
    checks.extend(_check_profile_artifacts())

    try:
        mcp_checks = asyncio.run(_check_mcp(cfg))
    except RuntimeError:
        mcp_checks = []
    checks.extend(mcp_checks)

    # Plugin-contributed checks + repairs (run last so plugins see a fully-
    # loaded registry, config, and DB-writable environment).
    try:
        contrib_checks = asyncio.run(_run_contributions(fix=fix))
    except RuntimeError:
        contrib_checks = []
    checks.extend(contrib_checks)

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
        console.print(f"[red bold]{failures} failure(s)[/red bold] — fix these before running.")
    elif warnings:
        console.print(f"[yellow bold]{warnings} warning(s)[/yellow bold] — should still work.")
    else:
        console.print("[green bold]All checks passed.[/green bold]")
    return failures


__all__ = ["run_doctor"]
