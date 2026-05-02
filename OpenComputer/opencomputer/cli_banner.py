"""OpenComputer welcome banner.

Visual + structure modeled after hermes-agent's banner.py.
Independently re-implemented on rich (no code copied).

Public API:
  - build_welcome_banner(console, model, cwd, *, session_id, home) -> None
  - format_banner_version_label() -> str
  - get_available_skills() -> dict[str, list[str]]
  - get_available_tools() -> dict[str, list[str]]
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from opencomputer import __version__

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "build_welcome_banner",
    "format_banner_version_label",
    "get_available_skills",
    "get_available_tools",
]


def _git_short_sha() -> str | None:
    """Return 7-char git SHA of HEAD, or None if not in a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).parent,
            text=True,
            timeout=2,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def format_banner_version_label() -> str:
    """`OpenComputer v0.1.0 · sha`."""
    sha = _git_short_sha()
    if sha:
        return f"OpenComputer v{__version__} · {sha}"
    return f"OpenComputer v{__version__}"


def _skill_search_paths() -> list[Path]:
    """Return ordered list of dirs to walk for SKILL.md files.

    Highest-priority first (so path 0 wins on duplicate names).
    """
    import os

    paths: list[Path] = []
    home = os.environ.get("OPENCOMPUTER_HOME")
    if home:
        paths.append(Path(home) / "skills")
    else:
        paths.append(Path.home() / ".opencomputer" / "skills")

    bundled = Path(__file__).parent / "skills"
    if bundled.exists():
        paths.append(bundled)

    return paths


def get_available_skills() -> dict[str, list[str]]:
    """Walk skill search paths; return {group: sorted-skill-names}.

    Group is the parent-of-SKILL.md directory's parent (one level up).
    Layout assumed: ``<root>/<group>/<skill>/SKILL.md``.
    """
    seen_per_group: dict[str, set[str]] = {}
    for root in _skill_search_paths():
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            skill_dir = skill_md.parent
            group_dir = skill_dir.parent
            group = root.name if group_dir == root else group_dir.name
            seen_per_group.setdefault(group, set()).add(skill_dir.name)
    return {g: sorted(s) for g, s in sorted(seen_per_group.items())}


def _tool_registry_snapshot() -> dict[str, str]:
    """Return mapping of tool_name -> plugin_name.

    Reads from opencomputer.tools.registry's module-level `registry`
    singleton. Since BaseTool instances don't carry a plugin_id field,
    we derive the group from the tool's module path:
      - opencomputer.tools.* → "core"
      - extensions.<plugin>.* → "<plugin>"
      - other → "other"
    """
    from opencomputer.tools.registry import registry

    out: dict[str, str] = {}
    for name in registry.names():
        tool = registry.get(name)
        if tool is None:
            continue
        module = type(tool).__module__ or ""
        if module.startswith("opencomputer.tools."):
            group = "core"
        elif module.startswith("extensions."):
            parts = module.split(".")
            group = parts[1] if len(parts) > 1 else "extensions"
        else:
            group = "other"
        out[name] = group
    return out


def get_available_tools() -> dict[str, list[str]]:
    """Group registered tools by plugin-of-origin. Empty dict if registry
    isn't reachable (e.g., before plugin discovery has run)."""
    try:
        snapshot = _tool_registry_snapshot()
    except Exception:  # noqa: BLE001
        return {}
    grouped: dict[str, list[str]] = {}
    for tool_name, plugin in snapshot.items():
        grouped.setdefault(plugin, []).append(tool_name)
    return {p: sorted(names) for p, names in sorted(grouped.items())}


_TIPS: tuple[str, ...] = (
    "Tip: `OPENCOMPUTER_EPHEMERAL_SYSTEM_PROMPT` injects a system prompt "
    "that's never persisted to history.",
    "Tip: Type `/help` for the slash-command list.",
    "Tip: Press Ctrl+C in chat to cancel the current turn cleanly.",
    "Tip: `oc -p <profile>` runs with a different active profile.",
    "Tip: `oc setup` re-runs the wizard — keeps your existing config "
    "by default.",
    "Tip: `/snapshot export` archives your session for later replay.",
)


def _truncate_csv(items: list[str], max_chars: int) -> str:
    """Return comma-separated items, truncated with `…` if over limit."""
    joined = ", ".join(items)
    if len(joined) <= max_chars:
        return joined
    out: list[str] = []
    used = 0
    ellipsis = ", …"
    budget = max_chars - len(ellipsis)
    for it in items:
        addition = (", " if out else "") + it
        if used + len(addition) > budget:
            break
        out.append(it)
        used += len(addition)
    return ", ".join(out) + ellipsis


def build_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    *,
    session_id: str | None = None,
    home: Path | None = None,
) -> None:
    """Print the OPENCOMPUTER welcome banner with categorized
    tools/skills listing."""
    import random

    from rich.text import Text

    from opencomputer.cli_banner_art import (
        OPENCOMPUTER_LOGO,
        OPENCOMPUTER_LOGO_FALLBACK,
        SIDE_GLYPH,
    )

    # 1. Logo (skip figlet if terminal too narrow)
    width = console.size.width if console.size else 80
    longest = max(
        (len(line) for line in OPENCOMPUTER_LOGO.splitlines() if line),
        default=0,
    )
    if width >= longest:
        console.print(Text(OPENCOMPUTER_LOGO, style="bold yellow"))
    else:
        console.print(Text(OPENCOMPUTER_LOGO_FALLBACK, style="bold yellow"))

    # 2. Version label
    label = format_banner_version_label()
    console.print(Text(label, style="dim yellow"), justify="right")

    # 3. Side glyph + meta block
    glyph_lines = SIDE_GLYPH.strip("\n").splitlines()
    for line in glyph_lines:
        console.print(Text(line, style="bold magenta"))
    console.print()
    console.print(f"[bold]{model}[/bold] · OpenComputer")
    console.print(f"[dim]{cwd}[/dim]")
    if session_id:
        console.print(f"[dim]Session: {session_id}[/dim]")
    if home:
        console.print(f"[dim]{home}[/dim]")

    # 4. Tools listing
    line_budget = max(40, width - 12)
    tools = get_available_tools()
    console.print()
    console.print("[bold]Available Tools[/bold]")
    for plugin in sorted(tools.keys()):
        names = tools[plugin]
        console.print(f"  [cyan]{plugin}:[/cyan] {_truncate_csv(names, line_budget)}")

    # 5. Skills listing
    skills = get_available_skills()
    console.print()
    console.print("[bold]Available Skills[/bold]")
    for group in sorted(skills.keys()):
        names = skills[group]
        console.print(f"  [magenta]{group}:[/magenta] {_truncate_csv(names, line_budget)}")

    # 6. Footer
    n_tools = sum(len(v) for v in tools.values())
    n_skills = sum(len(v) for v in skills.values())
    console.print()
    console.print(
        f"[dim]{n_tools} tools · {n_skills} skills · "
        f"[bold]/help[/bold] for commands[/dim]"
    )

    # 7. Welcome line
    console.print()
    console.print(
        "[bold]Welcome to OpenComputer![/bold] "
        "Type your message or /help for commands."
    )

    # 8. Tip
    if _TIPS:
        console.print(f"[dim]+ {random.choice(_TIPS)}[/dim]")
