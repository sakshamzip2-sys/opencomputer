"""OpenComputer welcome banner — Hermes-style splash, pink palette, OC data.

2026-05-12 redesign (third pass): integrates the hermes-agent visual
shape — chunky ``ansi_shadow`` wordmark over a rounded panel that puts
a Braille caduceus + runtime info on the left and Available Tools +
Available Skills on the right, then a single welcome line and a random
``✦ Tip:`` below — and pours OC's own data (real tool/skill registries,
real version + git SHA, real model/provider/cwd/session) into it. Colors
are OC pink (gold→amber→bronze swapped for hot-pink → rose → deep-rose).

Public API (kept stable; the call site in ``cli.py:_render_chat_banner``
passes these exact kwargs):

  - build_welcome_banner(console, model, cwd, *, provider=None,
      session_id=None, session_label=None, home=None) -> None
  - format_banner_version_label() -> str
  - get_available_skills() -> dict[str, list[str]]
  - get_available_tools() -> dict[str, list[str]]
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from opencomputer import __version__
from opencomputer.cli_banner_art import (
    OPEN_COMPUTER_CADUCEUS_PINK,
    OPEN_COMPUTER_LOGO_HERMES_STACKED,
    OPEN_COMPUTER_LOGO_HERMES_STACKED_WIDTH,
    OPEN_COMPUTER_LOGO_HERMES_STYLE,
    OPEN_COMPUTER_LOGO_HERMES_STYLE_WIDTH,
    OPENCOMPUTER_LOGO_FALLBACK,
)

if TYPE_CHECKING:
    from rich.console import Console

__all__ = [
    "build_welcome_banner",
    "format_banner_version_label",
    "get_available_skills",
    "get_available_tools",
]


# --- Palette -----------------------------------------------------------
# 3-tier OC pink gradient + neutral text + dim/gray for secondary info.
# These mirror the cli_banner_art constants and the visual reference in
# /Users/saksham/Vscode/claude/hermes_launch.py.
#
# Skin-aware (2026-05-17): the palette is resolved per render from the
# *active* skin's ``banner_*`` color block via :func:`_palette`, so
# ``oc skin set mono`` re-themes the splash. The ``default`` skin (and
# no active skin) always yield OC's pink, so the splash stays
# byte-identical to its historical output.
_FALLBACK: dict[str, str] = {
    "title": "#FF3D8A",    # hot pink — wordmark top + panel title (bold)
    "accent": "#E91E78",   # rose — section headers, model accent
    "border": "#C2185B",   # deep rose — panel border + wordmark bottom
    "dim": "#8E1A4F",      # dark rose — secondary prose, tip
    "text": "#E8E2D4",     # off-white — body text (tools, skills, values)
    "session": "#8B8682",  # warm gray — Session: line
}

#: Legacy module-attr name -> ``_FALLBACK`` key, consulted by ``__getattr__``
#: so external callers reading ``cli_banner._TITLE`` (and the historical
#: back-compat aliases) still resolve — now skin-aware.
_PALETTE_NAMES: dict[str, str] = {
    "_TITLE": "title",
    "_ACCENT": "accent",
    "_BORDER": "border",
    "_DIM": "dim",
    "_TEXT": "text",
    "_SESSION": "session",
    "_PRIMARY": "title",
    "_MUTED": "dim",
    "_ROSE_TEXT": "title",
    "_ROSE_ACCENT": "border",
    "_DIVIDER": "border",
}


def _palette() -> dict[str, str]:
    """Resolve the banner palette from the active skin.

    The ``default`` skin (and no active skin) yield OC's hardcoded pink,
    so the splash is byte-identical to its historical output. Any other
    skin themes the banner from its ``banner_*`` color block, falling
    back to OC pink for any key the skin omits. Never raises — a skin
    lookup failure collapses to the pink fallback.
    """
    try:
        from opencomputer.cli_ui.skin import current_spec

        spec = current_spec()
    except Exception:  # noqa: BLE001 — the splash must never crash on skins
        return dict(_FALLBACK)
    if spec is None or getattr(spec, "name", "") == "default":
        return dict(_FALLBACK)
    colors = getattr(spec, "colors", {}) or {}
    return {
        "title": colors.get("banner_title") or _FALLBACK["title"],
        "accent": colors.get("banner_accent") or _FALLBACK["accent"],
        "border": colors.get("banner_border") or _FALLBACK["border"],
        "dim": colors.get("banner_dim") or _FALLBACK["dim"],
        "text": colors.get("banner_text") or _FALLBACK["text"],
        "session": colors.get("session_border") or _FALLBACK["session"],
    }


def __getattr__(name: str) -> str:
    """Resolve the skin-aware legacy palette names (PEP 562).

    ``_TITLE`` and its siblings are not module globals — external reads
    resolve here, live, against the active skin via :func:`_palette`.
    The module's own render helpers call :func:`_palette` directly
    (a function's ``LOAD_GLOBAL`` never triggers module ``__getattr__``).
    Any other missing attribute raises ``AttributeError`` as usual.
    """
    key = _PALETTE_NAMES.get(name)
    if key is not None:
        return _palette()[key]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Layout knobs.
_WORDMARK_MIN_WIDTH = OPEN_COMPUTER_LOGO_HERMES_STYLE_WIDTH + 2
# Panel responsiveness — three tiers. At/above _PANEL_TWO_COL_MIN_WIDTH
# the laurel + runtime sit BESIDE tools/skills (two-column grid).
# Between _PANEL_MIN_WIDTH and that: one boxed column, hero stacked OVER
# the sections, so the narrow right side no longer shreds the tool/skill
# lists. Below _PANEL_MIN_WIDTH: drop the box (Rich borders eat too many
# cells to be worth it on a tiny terminal).
_PANEL_MIN_WIDTH = 44           # below this: no box, plain stacked text
_PANEL_TWO_COL_MIN_WIDTH = 88   # at/above this: two columns; below: single column
_TOOLS_MAX_TOOLSETS = 12        # show first N toolset rows
_SKILLS_MAX_CATEGORIES = 20     # show first N skill-category rows (matches Hermes density)
_PER_GROUP_CHAR_BUDGET = 60     # truncate ``cat: a, b, c, ...`` if items >N chars


# --- Helpers -----------------------------------------------------------


def _git_short_sha() -> str | None:
    """Return 7-char git SHA of HEAD, or None if not in a git repo.

    Fails open: ``git`` missing, slow, or returning non-zero all collapse
    to ``None`` so the splash still renders.
    """
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
    """``OpenComputer v{ver}`` or ``OpenComputer v{ver} · {sha}``."""
    if not __version__:
        return "OpenComputer"
    sha = _git_short_sha()
    if sha:
        return f"OpenComputer v{__version__} · {sha}"
    return f"OpenComputer v{__version__}"


def _skill_search_paths() -> list[Path]:
    """Return ordered list of dirs to walk for SKILL.md files."""
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
    """Walk skill search paths; return ``{group: sorted_skill_names}``.

    Group is derived from the directory layout
    ``<root>/<group>/<skill>/SKILL.md``. Duplicate skill names dedupe
    across search paths (first occurrence wins).
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
    """Return ``{tool_name: plugin_name}`` mapping.

    Derives plugin-of-origin from each tool's module path:
      - ``opencomputer.tools.*`` → ``"core"``
      - ``extensions.<plugin>.*`` → ``"<plugin>"``
      - anything else → ``"other"``
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
    """Group registered tools by plugin-of-origin. Empty dict if the
    registry isn't reachable (e.g., before plugin discovery has run).
    """
    try:
        snapshot = _tool_registry_snapshot()
    except Exception:  # noqa: BLE001 — registry init is best-effort
        return {}
    grouped: dict[str, list[str]] = {}
    for tool_name, plugin in snapshot.items():
        grouped.setdefault(plugin, []).append(tool_name)
    return {p: sorted(names) for p, names in sorted(grouped.items())}


def _split_model_provider(model: str, provider: str | None) -> tuple[str, str | None]:
    """Accept either explicit ``provider=`` or a ``"model (provider)"`` string.

    The original call site in ``cli.py`` previously concatenated provider
    into the model string; the new one passes ``provider=`` explicitly.
    Both shapes resolve to the same ``(model_clean, provider_clean)`` pair.
    """
    if provider:
        return model, provider
    if model.endswith(")") and "(" in model:
        head, _, rest = model.rpartition("(")
        prov = rest[:-1].strip()
        return head.strip(), prov or None
    return model, None


def _shorten_session(session_id: str, *, head: int = 8, tail: int = 7) -> str:
    """``64d3a534-…-b68d749`` → ``64d3a534…b68d749`` (16-char display)."""
    stripped = session_id.replace("-", "")
    if len(stripped) <= head + tail + 1:
        return session_id
    return f"{stripped[:head]}…{stripped[-tail:]}"


def _truncate_items_list(items: list[str], budget: int) -> str:
    """``a, b, c, d, …`` capped at ``budget`` chars, ellipsis on overflow."""
    if not items:
        return ""
    full = ", ".join(items)
    if len(full) <= budget:
        return full
    out: list[str] = []
    used = 0
    for item in items:
        addition = f"{', ' if out else ''}{item}"
        if used + len(addition) + 5 > budget:  # 5 = ``, ...``
            break
        out.append(item)
        used += len(addition)
    suffix = ", ..." if out else "..."
    return ", ".join(out) + suffix


def _format_group_line(group: str, items: list[str]) -> str:
    """One Rich-markup row: ``[dim DIM]group:[/] [TEXT]a, b, c, ...[/]``."""
    pal = _palette()
    items_str = _truncate_items_list(sorted(items), _PER_GROUP_CHAR_BUDGET)
    return f"[dim {pal['dim']}]{group}:[/] [{pal['text']}]{items_str}[/]"


def _categorize_skills_by_prefix(
    grouped: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Re-bucket flat-layout skills by first-hyphen-segment prefix.

    OC's ``get_available_skills()`` returns ``{"skills": [name, ...]}``
    when the on-disk layout is flat (``~/.opencomputer/skills/<skill>/
    SKILL.md`` — no category subdir). The splash needs the
    Hermes-style multi-row look, so we derive categories from the
    skill names themselves: ``apple-notes`` + ``apple-reminders``
    bucket under ``apple``; ``github-auth`` + ``github-pr`` under
    ``github``; unique single-skill prefixes fall into a ``general``
    bucket so we don't render 79 rows of 1 skill each.

    No-op when the input already has multiple groups (the caller has
    a real category-aware layout).
    """
    if len(grouped) > 1:
        return grouped
    flat = [name for names in grouped.values() for name in names]
    if not flat:
        return grouped
    by_prefix: dict[str, list[str]] = {}
    for name in flat:
        prefix = name.split("-", 1)[0] if "-" in name else "general"
        by_prefix.setdefault(prefix, []).append(name)
    multi: dict[str, list[str]] = {}
    leftover: list[str] = []
    for prefix, names in by_prefix.items():
        if len(names) >= 2 and prefix != "general":
            multi[prefix] = sorted(names)
        else:
            leftover.extend(names)
    if leftover:
        multi["general"] = sorted(leftover)
    return dict(sorted(multi.items()))


# --- Tip rotation (OC-flavored) ---------------------------------------
# Single-color dim rendering with a ``✦`` prefix — matches Hermes' format.
# Every tip references a real OC command or env var, verified to exist
# (no liar UI — invariant matches the slash-command footer audit).
_TIPS: tuple[str, ...] = (
    "Type /help for the full slash-command list.",
    "oc -p <profile> runs with a different active profile.",
    "/snapshot export archives your session for later replay.",
    "Press Ctrl+C in chat to cancel the current turn cleanly.",
    "oc setup re-runs the wizard — keeps your existing config by default.",
    "OPENCOMPUTER_EPHEMERAL_SYSTEM_PROMPT injects a system prompt that's never persisted.",
    "oc resume last reopens the most recent session.",
    "oc profile list shows every profile this install knows about.",
    "/model picks a different provider/model mid-session.",
)


# --- Splash sections --------------------------------------------------


def _build_version_cluster() -> tuple[str, str]:
    """Return ``(visible_text, rich_markup)`` for the right-side
    ``v{ver} · {sha}`` cluster. ``("", "")`` when there's nothing.
    """
    if not __version__:
        return "", ""
    pal = _palette()
    label = f"v{__version__}"
    visible = label
    markup = f"[bold {pal['title']}]{label}[/]"
    sha = _git_short_sha()
    if sha:
        visible += f" · {sha}"
        markup += f"[{pal['dim']}] · {sha}[/]"
    return visible, markup


def _active_profile_name() -> str | None:
    """Return the current sticky profile name, or None for ``default``.

    Fails open: any import error or read failure returns None so the
    splash still renders.
    """
    try:
        from opencomputer.profiles import read_active_profile

        name = read_active_profile()
        if name and name != "default":
            return name
    except Exception:  # noqa: BLE001 — profile read is best-effort
        return None
    return None


def _build_left_column(
    model: str,
    provider: str | None,
    cwd: str,
    session_id: str | None,
    session_label: str | None,
):
    """Composite renderable: centered caduceus + left-aligned runtime info.

    The two halves use different justifications, so we return a Rich
    ``Group`` of (centered Text) → (blank) → (Text-aligned left). Using
    ``Align`` lets each block keep its own justification inside the
    panel's left column, instead of inheriting the column's
    ``justify="center"`` which would also center the model/cwd lines.
    """
    from rich.align import Align
    from rich.console import Group
    from rich.text import Text

    pal = _palette()
    caduceus = Align.center(
        Text.from_markup(OPEN_COMPUTER_CADUCEUS_PINK, end="")
    )

    runtime_lines: list[str] = []
    model_clean, provider_clean = _split_model_provider(model, provider)
    model_short = model_clean.split("/")[-1] if "/" in model_clean else model_clean
    if model_short.endswith(".gguf"):
        model_short = model_short[:-5]
    if len(model_short) > 28:
        model_short = model_short[:25] + "..."

    if model_short:
        accent_line = f"[{pal['accent']}]{model_short}[/]"
        if provider_clean:
            accent_line += f" [dim {pal['dim']}]· {provider_clean}[/]"
        runtime_lines.append(accent_line)

    if cwd:
        runtime_lines.append(f"[dim {pal['dim']}]{cwd}[/]")

    if session_id:
        # When the caller's helper echoed back the raw uuid as the
        # ``session_label`` (its "no title yet" sentinel), treat that as
        # "no real label" and elide the uuid instead.
        if session_label and session_label != session_id:
            shown = session_label
        else:
            shown = _shorten_session(session_id)
        runtime_lines.append(f"[dim {pal['session']}]Session: {shown}[/]")

    profile_name = _active_profile_name()
    if profile_name:
        runtime_lines.append(
            f"[bold {pal['accent']}]Profile:[/] [{pal['text']}]{profile_name}[/]"
        )

    if not runtime_lines:
        return Group(caduceus)

    runtime_block = Align.left(
        Text.from_markup("\n".join(runtime_lines), end="")
    )

    return Group(caduceus, Text(""), runtime_block)


def _build_right_column(
    tools_grouped: dict[str, list[str]],
    skills_grouped: dict[str, list[str]],
    mcp_status: list[dict] | None = None,
) -> str:
    """``Available Tools`` + per-toolset rows + ``Available Skills`` +
    per-category rows + optional ``MCP Servers`` section + summary line.
    """
    pal = _palette()
    lines: list[str] = [f"[bold {pal['accent']}]Available Tools[/]"]

    sorted_toolsets = sorted(tools_grouped.items()) if tools_grouped else []
    for toolset, names in sorted_toolsets[:_TOOLS_MAX_TOOLSETS]:
        lines.append(_format_group_line(toolset, names))
    extra_toolsets = max(0, len(sorted_toolsets) - _TOOLS_MAX_TOOLSETS)
    if extra_toolsets > 0:
        plural = "toolset" if extra_toolsets == 1 else "toolsets"
        lines.append(f"[dim {pal['dim']}](and {extra_toolsets} more {plural}...)[/]")

    lines.append("")
    lines.append(f"[bold {pal['accent']}]Available Skills[/]")

    sorted_skills = sorted(skills_grouped.items()) if skills_grouped else []
    for category, names in sorted_skills[:_SKILLS_MAX_CATEGORIES]:
        lines.append(_format_group_line(category, names))
    extra_categories = max(0, len(sorted_skills) - _SKILLS_MAX_CATEGORIES)
    if extra_categories > 0:
        plural = "category" if extra_categories == 1 else "categories"
        lines.append(f"[dim {pal['dim']}](and {extra_categories} more {plural}...)[/]")

    # MCP Servers — only rendered when at least one is known. Matches
    # upstream Hermes' conditional section (banner.py:536-549). Three
    # states drive the visual:
    #   - "connected"  → bumps the summary count, shows tool count
    #   - "configured" → declared in config, not yet connected (banner
    #                    renders before MCPManager.connect_all). Neutral
    #                    color, no count, no error message.
    #   - anything else → red + error message (live snapshot path).
    mcp_connected = 0
    mcp_configured = 0
    if mcp_status:
        lines.append("")
        lines.append(f"[bold {pal['accent']}]MCP Servers[/]")
        for srv in mcp_status:
            name = srv.get("name", "unknown")
            transport = srv.get("transport") or srv.get("url", "")
            transport_short = transport[:24] + "…" if len(transport) > 25 else transport
            state = (srv.get("connection_state") or "").lower()
            if state == "connected" or srv.get("connected"):
                mcp_connected += 1
                tool_count = srv.get("tool_count", srv.get("tools", 0))
                if isinstance(tool_count, list):
                    tool_count = len(tool_count)
                plural = "tool" if tool_count == 1 else "tools"
                lines.append(
                    f"[dim {pal['dim']}]{name}[/] [{pal['text']}]({transport_short})[/] "
                    f"[dim {pal['dim']}]—[/] [{pal['text']}]{tool_count} {plural}[/]"
                )
            elif state == "configured":
                mcp_configured += 1
                lines.append(
                    f"[dim {pal['dim']}]{name}[/] [{pal['text']}]({transport_short})[/] "
                    f"[dim {pal['dim']}]— configured[/]"
                )
            else:
                err = srv.get("last_error") or "disconnected"
                lines.append(
                    f"[red]{name}[/] [dim]({transport_short})[/] [red]— {err}[/]"
                )

    if not sorted_toolsets and not sorted_skills and not mcp_status:
        # Everything empty — most likely plugin discovery hasn't run
        # yet (e.g., direct test import). Don't render a misleading
        # ``0 tools, 0 skills`` line.
        return "\n".join(lines)

    n_tools = sum(len(v) for v in tools_grouped.values()) if tools_grouped else 0
    n_skills = sum(len(v) for v in skills_grouped.values()) if skills_grouped else 0
    lines.append("")
    summary_parts = [f"{n_tools} tools", f"{n_skills} skills"]
    if mcp_connected:
        summary_parts.append(f"{mcp_connected} MCP")
    elif mcp_configured:
        # No live connections yet (banner-time), but config declares N
        # MCP servers. Surface the count so users see the section is
        # populated even when servers haven't connected.
        plural = "MCP" if mcp_configured == 1 else "MCP"
        summary_parts.append(f"{mcp_configured} {plural}")
    lines.append(
        f"[dim {pal['dim']}]{' · '.join(summary_parts)} · "
        f"[/][{pal['accent']}]/help[/][dim {pal['dim']}] for commands[/]"
    )

    return "\n".join(lines)


def _render_wordmark(console: Console, term_width: int) -> None:
    """Render the colored Hermes-style ``ansi_shadow`` wordmark above the
    panel. Wide terminals get the one-line ``OPEN-COMPUTER`` logo;
    narrower ones get the same chunky font stacked ``OPEN`` over
    ``COMPUTER``; a pathologically narrow terminal falls back to plain
    text.
    """
    from rich.text import Text

    pal = _palette()
    if term_width >= _WORDMARK_MIN_WIDTH:
        console.print(
            Text.from_markup(OPEN_COMPUTER_LOGO_HERMES_STYLE),
            no_wrap=True,
            overflow="ignore",
            soft_wrap=False,
        )
        return

    # Stacked Hermes-style fallback — same ``ansi_shadow`` font, OPEN
    # over COMPUTER, so the chunky wordmark still fits terminals too
    # narrow for the 110-col one-line logo. The stacked art is exactly
    # OPEN_COMPUTER_LOGO_HERMES_STACKED_WIDTH cols, so render it down to
    # that exact width — it fits edge-to-edge and never wraps (no_wrap
    # below). This keeps it aligned with the panel, which boxes from the
    # same width.
    if term_width >= OPEN_COMPUTER_LOGO_HERMES_STACKED_WIDTH:
        console.print(
            Text.from_markup(OPEN_COMPUTER_LOGO_HERMES_STACKED),
            no_wrap=True,
            overflow="ignore",
            soft_wrap=False,
        )
        return

    # Pathological narrow: plain bold "OPENCOMPUTER".
    console.print(
        Text(OPENCOMPUTER_LOGO_FALLBACK, style=f"bold {pal['title']}"),
        no_wrap=True,
        overflow="ellipsis",
    )


def _render_panel(
    console: Console,
    *,
    model: str,
    cwd: str,
    provider: str | None,
    session_id: str | None,
    session_label: str | None,
    mcp_status: list[dict] | None,
    term_width: int,
) -> None:
    """Render the responsive welcome panel — three width tiers.

    * ``>= _PANEL_TWO_COL_MIN_WIDTH`` — boxed, two columns: laurel +
      runtime beside tools/skills/mcp.
    * ``>= _PANEL_MIN_WIDTH`` — boxed, single column: the same blocks
      stacked, so the tool/skill lists get the panel's full inner width
      instead of being crushed into a ~35-cell right column.
    * below ``_PANEL_MIN_WIDTH`` — no box, plain stacked text (Rich
      borders eat too many cells to be worth it on a tiny terminal —
      CI logs, mobile SSH).
    """
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    pal = _palette()
    left = _build_left_column(model, provider, cwd, session_id, session_label)
    # Apply the prefix-derived recategorization to skills so a flat
    # ``{"skills": [139 names]}`` registry shows as multiple rows
    # (apple, github, opencomputer, …) instead of a single line.
    skills_grouped = _categorize_skills_by_prefix(get_available_skills() or {})
    right = _build_right_column(
        get_available_tools() or {},
        skills_grouped,
        mcp_status=mcp_status,
    )

    if term_width < _PANEL_MIN_WIDTH:
        # Stacked, unboxed — Rich Panel borders eat too many cells on
        # truly tiny terminals.
        console.print(left, highlight=False)
        console.print()
        console.print(right, highlight=False)
        return

    title = f"[bold {pal['title']}]{format_banner_version_label()}[/]"

    if term_width < _PANEL_TWO_COL_MIN_WIDTH:
        # Single-column boxed — below ~88 cols the 31-cell laurel art
        # leaves the two-column right side too narrow and the tool /
        # skill lists shred into 2-3 word fragments. Stack the hero
        # over the sections so each gets the panel's full inner width.
        body = Group(left, Text(""), Text.from_markup(right, end=""))
    else:
        # Two-column grid — laurel + runtime beside tools/skills/mcp.
        # ``_build_left_column`` is internally aligned (caduceus
        # centered, runtime left), so the grid leaves ``justify`` at its
        # default and lets the contents align themselves.
        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column(justify="left")
        table.add_row(left, right)
        body = table

    # padding=(0, 1) — 2-cell horizontal padding inside the border added
    # whitespace without serving the layout. In the two-column body the
    # grid's own ``padding=(0, 2)`` still spaces hero from sections.
    panel = Panel(
        body,
        title=title,
        border_style=pal["border"],
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)


def _render_welcome_and_tip(console: Console) -> None:
    """``Welcome to OpenComputer!`` line + a single random ``✦ Tip:``."""
    from rich.text import Text

    pal = _palette()
    console.print(
        Text.from_markup(
            f"[{pal['text']}]Welcome to OpenComputer! Type your message or "
            f"/help for commands.[/]"
        ),
        highlight=False,
    )
    if not _TIPS:
        return
    tip = random.choice(_TIPS)
    console.print(
        Text.from_markup(f"[dim {pal['dim']}]✦ Tip: {tip}[/]"),
        highlight=False,
    )


def _render_update_hint(console: Console) -> None:
    """Optional one-line ``+ N commits behind`` hint, warn-yellow.

    Yellow (not pink) so it doesn't blend with the brand palette —
    matches upstream Hermes' ``[bold yellow]⚠ N commits behind[/]``.
    Fails open: any import/call error swallows silently so a wedged
    update-check never wedges ``oc chat`` startup.
    """
    try:
        from opencomputer.cli_update_check import get_update_hint

        hint = get_update_hint(timeout=0.2)
        if hint:
            console.print(f"[bold yellow]⚠ {hint}[/]", highlight=False)
    except Exception:  # noqa: BLE001 — splash must never crash on hints
        pass


# --- Public entry point ------------------------------------------------


def build_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    *,
    provider: str | None = None,
    session_id: str | None = None,
    session_label: str | None = None,
    home: Path | None = None,
    mcp_status: list[dict] | None = None,
) -> None:
    """Print the OpenComputer welcome banner — Hermes-style splash, OC data.

    Layout (top to bottom):

      1. Pink Hermes-style ``ansi_shadow`` wordmark — one-line
         ``OPEN-COMPUTER`` (≥112 cols) or stacked ``OPEN``/``COMPUTER``
         (≥72 cols), with a plain-text fallback below.
      2. Rounded panel titled ``OpenComputer v{ver} · {sha}`` —
         responsive: two columns side-by-side when wide, one stacked
         column when narrower, unboxed plain text on a tiny terminal:
         - left column: Braille caduceus (centered) over a left-aligned
           runtime block: ``{model}`` (accent) ``· {provider}`` (dim),
           ``{cwd}`` (dim), ``Session: {label}`` (gray), and a
           ``Profile: {name}`` line when a non-default profile is sticky.
         - right column: ``Available Tools`` (top 8 toolsets), ``Available
           Skills`` (top 8 categories), ``MCP Servers`` (only when at
           least one is provided), summary footer ``N tools · N skills
           · M MCP · /help for commands``.
      3. ``Welcome to OpenComputer!`` line + a random ``✦ Tip:`` (every
         tip references a real OC command or env var).
      4. Optional ``⚠ N commits behind`` hint when an update is detected.

    Args:
        console: Rich Console to render into.
        model: Active model name. Bare (``claude-opus-4-7``) or combined
            (``claude-opus-4-7 (anthropic)``) form both work.
        cwd: Working directory string (or "" to omit).
        provider: Explicit provider override; preferred over inferring
            from the combined ``model`` form.
        session_id: UUID of the active session, or None for fresh.
        session_label: Human-readable title for the session if one has
            been set; falls back to an elided UUID otherwise.
        home: OPENCOMPUTER_HOME path — accepted for back-compat with the
            previous splash signature, not rendered.
        mcp_status: Optional list of MCP server status dicts (matches
            ``MCPManager.status_snapshot`` shape). When provided, the
            panel adds an ``MCP Servers`` section showing each server's
            transport + connection state + tool count. ``None`` or
            empty list suppresses the section entirely.
    """
    _ = home  # accepted for API stability; intentionally not rendered
    term_width = console.size.width if console.size else 100

    console.print()  # top spacer
    _render_wordmark(console, term_width)
    console.print()

    _render_panel(
        console,
        model=model,
        cwd=cwd,
        provider=provider,
        session_id=session_id,
        session_label=session_label,
        mcp_status=mcp_status,
        term_width=term_width,
    )

    _render_welcome_and_tip(console)
    _render_update_hint(console)
