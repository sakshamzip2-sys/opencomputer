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


_BLOCK_LOGO_WIDTH = 105  # widest line of OPENCOMPUTER_BLOCK_LOGO

# Side mascot — a clean little robot rendered in yellow on the left
# of the info panel (Hermes-screenshot layout). Designed to read as
# a "computer agent" at a glance: head with eyes + antenna, torso
# with status LEDs, two stubby arms.
_OC_MASCOT = (
    "          ▄▄▄          \n"
    "         ╱   ╲         \n"
    "        ╱_____╲        \n"
    "       ┌───────┐       \n"
    "       │ ◉   ◉ │       \n"
    "       │   ▽   │       \n"
    "       │  ───  │       \n"
    "       └───┬───┘       \n"
    "      ╔════╧════╗      \n"
    "    ◀═╣ ▣  ▣  ▣ ╠═▶    \n"
    "      ║ ▒▒▒▒▒▒▒ ║      \n"
    "      ║  oc-01  ║      \n"
    "      ╚═══╤═╤═══╝      \n"
    "         ─┘ └─         \n"
    "                       \n"
    "     OpenComputer      \n"
    "       /agent          \n"
)

_MAX_GROUPS_SHOWN = 8       # how many tool/skill groups to list inline
_MAX_ITEMS_PER_GROUP = 4    # how many items per group before "..."


def _format_group_line(group: str, items: list[str]) -> str:
    """Render one ``group: item1, item2, ...`` line, truncated."""
    shown = items[:_MAX_ITEMS_PER_GROUP]
    csv = ", ".join(shown)
    if len(items) > _MAX_ITEMS_PER_GROUP:
        csv += ", ..."
    return f"  [cyan]{group}[/cyan]: [dim]{csv}[/dim]"


def _render_groups(grouped: dict[str, list[str]]) -> tuple[list[str], int]:
    """Render up to ``_MAX_GROUPS_SHOWN`` groups; return (lines, total_items)."""
    total = sum(len(v) for v in grouped.values())
    keys = list(grouped.keys())
    lines = [_format_group_line(k, grouped[k]) for k in keys[:_MAX_GROUPS_SHOWN]]
    extra = len(keys) - _MAX_GROUPS_SHOWN
    if extra > 0:
        lines.append(f"  [dim](and {extra} more group{'s' if extra != 1 else ''}…)[/dim]")
    return lines, total


def build_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    *,
    session_id: str | None = None,
    session_label: str | None = None,
    home: Path | None = None,
) -> None:
    """Print the OPENCOMPUTER welcome banner — Hermes-style:

    1. Big chunky ANSI-Shadow title (yellow, centered).
    2. Side-by-side block: a yellow ASCII mascot on the left, a cyan
       info panel on the right (version + tools + skills, all
       truncated so the panel never gets unwieldy).
    3. Welcome line + a tip.
    """
    import random

    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from opencomputer.cli_banner_art import OPENCOMPUTER_BLOCK_LOGO

    # Render the chunky single-line OPENCOMPUTER title. We always show
    # it: on terminals wider than the logo we center it; on narrower
    # terminals we print it un-centered (each row preserved) so the
    # chunky look persists even when the user's terminal is tight. The
    # ``no_wrap`` + ``overflow="ignore"`` keeps Rich from soft-wrapping
    # mid-glyph and breaking the figlet rows.
    width = console.size.width if console.size else 80
    logo = Text(
        OPENCOMPUTER_BLOCK_LOGO.rstrip("\n"),
        style="bold yellow",
        no_wrap=True,
        overflow="ignore",
    )
    if width >= _BLOCK_LOGO_WIDTH + 2:
        console.print(Align.center(logo))
    else:
        console.print(logo, soft_wrap=True, no_wrap=True, overflow="ignore")

    # Build the info panel content — version line, tools, skills, then
    # model/cwd/session. The grouping helpers return empty lists when
    # the registry hasn't been initialized yet, which is fine.
    sha = _git_short_sha() or ""
    version_line = f"[bold yellow]OpenComputer[/bold yellow] [dim]v{__version__}[/dim]"
    if sha:
        version_line += f" [dim]· {sha}[/dim]"

    panel_lines: list[str] = [version_line, ""]

    tools_grouped = get_available_tools()
    if tools_grouped:
        tool_lines, n_tools = _render_groups(tools_grouped)
        panel_lines.append("[bold]Available Tools[/bold]")
        panel_lines.extend(tool_lines)
        panel_lines.append("")

    skills_grouped = get_available_skills()
    if skills_grouped:
        skill_lines, n_skills = _render_groups(skills_grouped)
        panel_lines.append("[bold]Available Skills[/bold]")
        panel_lines.extend(skill_lines)
        panel_lines.append("")
    else:
        n_skills = 0
    n_tools = sum(len(v) for v in tools_grouped.values()) if tools_grouped else 0

    panel_lines.append(f"[bold]Model:[/bold]   {model}")
    panel_lines.append(f"[bold]CWD:[/bold]     [dim]{cwd}[/dim]")
    if session_id:
        shown_session = session_label or session_id
        panel_lines.append(f"[bold]Session:[/bold] [dim]{shown_session}[/dim]")
    if n_tools or n_skills:
        panel_lines.append("")
        panel_lines.append(
            f"[dim]{n_tools} tool{'s' if n_tools != 1 else ''} · "
            f"{n_skills} skill{'s' if n_skills != 1 else ''} · "
            f"/help for commands[/dim]"
        )
    _ = home  # accepted for backwards-compat; not rendered

    panel_body = Group(*[Text.from_markup(line) for line in panel_lines])
    info_panel = Panel(
        panel_body,
        border_style="cyan",
        padding=(0, 2),
        expand=False,
    )

    # Side-by-side: yellow mascot on the left, info panel on the right.
    # Falls back to stacked rendering on narrow terminals.
    mascot = Text(_OC_MASCOT.rstrip("\n"), style="bold yellow")
    if width >= 100:
        console.print(Columns([mascot, info_panel], padding=(0, 2)))
    else:
        console.print(mascot)
        console.print(info_panel)

    # Update-check hint — non-blocking (200ms), silently None when the
    # background check hasn't finished yet (caller already invoked
    # prefetch_update_check at startup).
    try:
        from opencomputer.cli_update_check import get_update_hint
        hint = get_update_hint(timeout=0.2)
        if hint:
            console.print(f"[yellow]+ {hint}[/yellow]")
    except Exception:  # noqa: BLE001
        pass  # update check is purely informational; never block startup

    # Welcome line — Hermes-parity wording.
    console.print()
    console.print(
        "[bold]Welcome to OpenComputer![/bold] "
        "Type your message or [cyan]/help[/cyan] for commands."
    )

    # Tip
    if _TIPS:
        console.print(f"[dim]+ {random.choice(_TIPS)}[/dim]")
