"""OpenComputer welcome banner — OC-style minimal splash.

The 2026-05-12 redesign replaces the previous "Option D HUD" splash
(mascot + 4-column runtime grid + tool/skill chip rows) with the
OpenCode-style minimal layout: ``OPENCOMPUTER`` half-block wordmark with
version pulled to the right, then a single footer row pointing at the
slash commands users need. Runtime state (model / provider / cwd /
session) lives in the statusline and the ``oc status`` slash command,
not on the splash. See
``docs/superpowers/specs/2026-05-12-oc-splash-replace-hermes-design.md``.

Public API (kept stable across the migration):
  - build_welcome_banner(console, model, cwd, *, provider=None,
      session_id=None, session_label=None, home=None) -> None
  - format_banner_version_label() -> str
  - get_available_skills() -> dict[str, list[str]]
  - get_available_tools() -> dict[str, list[str]]
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from opencomputer import __version__
from opencomputer.cli_banner_art import (
    OPENCOMPUTER_BLOCK_LOGO,
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
# Tokens mirror the previous splash so that downstream theme tooling that
# inspects ``cli_banner._PRIMARY`` / ``_FG`` / ``_MUTED`` keeps working.
_PRIMARY = "#FF3D8A"
_FG = "#E8E2D4"
_MUTED = "#7A7367"
_BORDER = "#4A463D"
# Back-compat aliases (kept so external ``from opencomputer.cli_banner
# import _ROSE_TEXT`` doesn't break).
_ROSE_TEXT = _PRIMARY
_ROSE_ACCENT = "#C2185B"
_DIVIDER = _BORDER


# --- Helpers -----------------------------------------------------------


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

    Group is the parent-of-SKILL.md directory's parent. Layout assumed:
    ``<root>/<group>/<skill>/SKILL.md``.
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

    Derives plugin-of-origin from the tool's module path:
      - ``opencomputer.tools.*`` → ``"core"``
      - ``extensions.<plugin>.*`` → ``"<plugin>"``
      - other → ``"other"``
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


# --- Splash internals --------------------------------------------------


# 71-col × 3-row half-block ``OPENCOMPUTER`` wordmark from ``cli_banner_art``.
_BLOCK_LOGO_ROWS = tuple(OPENCOMPUTER_BLOCK_LOGO.rstrip("\n").splitlines())
_BLOCK_LOGO_WIDTH = max((len(r) for r in _BLOCK_LOGO_ROWS), default=0)
# A version cluster of ``v0.1.0 · 1234abc`` is ~17 chars. Reserve a 2-char
# gap before it; below that minimum, drop the block logo and fall back.
_VERSION_RESERVE_MIN = 19
_BLOCK_LOGO_MIN_WIDTH = _BLOCK_LOGO_WIDTH + 2  # one col of left padding + at least 1 right

# The version sits on the middle row of the 3-row logo so the eye reads
# the wordmark and the version as one cluster.
_VERSION_ANCHOR_ROW = 1

# Footer copy — verbatim from the spec.
_FOOTER_LEFT_READY = "› Ready."
_FOOTER_LEFT_HINT_LEAD = "  Type a message, or "
_FOOTER_LEFT_HINT_CMD = "/help"
_FOOTER_RIGHT = "/status · /model · /help · /exit"


def _build_version_cluster() -> tuple[str, str]:
    """Return ``(visible_text, rich_markup)`` for the right-side
    ``v{ver} · {sha}`` cluster. Returns ``("", "")`` when there's nothing
    to render (e.g., ``__version__`` is empty).
    """
    if not __version__:
        return "", ""
    label = f"v{__version__}"
    visible = label
    markup = f"[bold {_PRIMARY}]{label}[/]"
    sha = _git_short_sha()
    if sha:
        visible += f" · {sha}"
        markup += f"[{_MUTED}] · {sha}[/]"
    return visible, markup


def _render_block_logo(console: Console, term_width: int) -> None:
    """Render the 3-row ``OPENCOMPUTER`` half-block wordmark in primary,
    with the version cluster pulled to the right edge on the middle row.
    """
    from rich.text import Text

    version_visible, version_markup = _build_version_cluster()

    for i, row in enumerate(_BLOCK_LOGO_ROWS):
        line = Text(no_wrap=True, overflow="ignore")
        line.append(row, style=_PRIMARY)
        if i == _VERSION_ANCHOR_ROW and version_visible:
            used = line.cell_len
            pad = max(2, term_width - used - len(version_visible))
            line.append(" " * pad)
            line.append(Text.from_markup(version_markup))
        console.print(line, soft_wrap=True, no_wrap=True, overflow="ignore")


def _render_text_fallback(console: Console, term_width: int) -> None:
    """Narrow-terminal fallback: render ``OPENCOMPUTER`` as bold text and
    drop the half-block art. Version cluster (if any) goes on a second
    line so we don't clip on very narrow terminals.

    At pathologically narrow widths we have to drop pieces, not clip
    them: ``v2026.5.10.post3 · e71380d`` truncated to ``v2026.5.10.po``
    is a worse experience than ``v2026.5.10.post3`` alone. So we try the
    full cluster first; if it doesn't fit, fall back to just ``v{ver}``;
    if even that doesn't fit, drop the version entirely.
    """
    from rich.text import Text

    title = Text(no_wrap=True, overflow="ellipsis")
    title.append(OPENCOMPUTER_LOGO_FALLBACK, style=f"bold {_PRIMARY}")
    console.print(title, soft_wrap=False, no_wrap=True, overflow="ellipsis")

    version_visible, version_markup = _build_version_cluster()
    if not version_visible:
        return
    if len(version_visible) <= term_width:
        # Full cluster fits — render it (right-aligned if room).
        ver = Text.from_markup(version_markup)
        pad = max(0, term_width - len(version_visible))
        console.print(
            Text(" " * pad) + ver, no_wrap=True, overflow="ignore"
        )
        return
    # Full cluster overflows. Try just ``v{ver}`` without the SHA.
    short_label = f"v{__version__}"
    if __version__ and len(short_label) <= term_width:
        console.print(
            f"[bold {_PRIMARY}]{short_label}[/]",
            highlight=False, no_wrap=True, overflow="ignore",
        )
        return
    # Even ``v{ver}`` overflows — drop the version line entirely rather
    # than show a half-truncated string.


def _render_footer(console: Console, term_width: int) -> None:
    """Render the single-line footer.

    Layout:
        ``› Ready.  Type a message, or /help    /status · /models · ...``

    When the terminal is narrower than the combined width of the two
    clusters, the right cluster wraps onto its own line — Rich handles
    the wrap automatically because we use ``soft_wrap=True``.
    """
    from rich.text import Text

    left_visible = (
        _FOOTER_LEFT_READY + _FOOTER_LEFT_HINT_LEAD + _FOOTER_LEFT_HINT_CMD
    )
    gap = max(2, term_width - len(left_visible) - len(_FOOTER_RIGHT))
    footer = Text.from_markup(
        f"[bold {_PRIMARY}]{_FOOTER_LEFT_READY[:1]}[/]"
        f"[bold {_FG}]{_FOOTER_LEFT_READY[1:]}[/]"
        f"[{_MUTED}]{_FOOTER_LEFT_HINT_LEAD}[/]"
        f"[{_FG}]{_FOOTER_LEFT_HINT_CMD}[/]"
        f"{' ' * gap}"
        f"[{_MUTED}]{_FOOTER_RIGHT}[/]"
    )
    console.print(footer, highlight=False, soft_wrap=True)


def _render_update_hint(console: Console) -> None:
    """Render the upgrade-available hint below the logo if one is set.

    Fails open: any import or call error swallows silently — a stale
    hint must never wedge ``oc chat`` startup.
    """
    try:
        from opencomputer.cli_update_check import get_update_hint

        hint = get_update_hint(timeout=0.2)
        if hint:
            console.print(f"[{_PRIMARY}]+ {hint}[/]", highlight=False)
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
) -> None:
    """Print the OpenComputer welcome banner.

    OC-style minimal splash (2026-05-12 redesign): the ``OPENCOMPUTER``
    half-block wordmark with the version + git SHA pulled to the right
    edge of its middle row, then a footer pointing at the slash commands
    users need to look up their runtime state.

    ``model``, ``cwd``, ``provider``, ``session_id``, ``session_label``,
    ``home`` are accepted for back-compat with the previous "Option D"
    splash signature but are intentionally NOT rendered. Runtime state
    lives in the statusline and ``oc status`` slash command. See
    ``docs/superpowers/specs/2026-05-12-oc-splash-replace-hermes-design.md``.
    """
    term_width = console.size.width if console.size else 100
    # Silence Ruff's unused-arg warning for kwargs preserved for API
    # stability. They are deliberately not rendered.
    _ = (model, cwd, provider, session_id, session_label, home)

    # Top spacer — one blank row so the logo doesn't hug the prompt
    # scrollback.
    console.print()

    if term_width >= _BLOCK_LOGO_MIN_WIDTH:
        _render_block_logo(console, term_width)
    else:
        _render_text_fallback(console, term_width)

    # Spacer between logo and footer.
    console.print()

    _render_update_hint(console)
    _render_footer(console, term_width)
    console.print()
