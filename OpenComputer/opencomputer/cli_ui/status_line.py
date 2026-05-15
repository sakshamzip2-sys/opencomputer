"""Claude-Code-style status line for ``oc chat``.

Renders a single bottom-bar row mirroring Claude Code's UX:

    ⚕ claude-opus-4-7 │ 12.4K/200K │ [██████░░░░] 6% │ $0.06 │ 15m

Components:
  1. ``⚕ `` (U+2695) prefix
  2. Model id (from ``runtime.custom["model_id"]``)
  3. Tokens used / max-context — formatted ``12.4K`` / ``200K``
  4. 10-char unicode progress bar (U+2588 + U+2591)
  5. Integer percentage
  6. Cost ``$X.YY`` from session-cumulative cost (omitted when missing)
  7. Elapsed time ``45s`` / ``15m`` / ``1h23m`` from session start

Production-grade rules:
- O(1) reads from ``runtime.custom`` only — render fires every keystroke.
- Cold start (zero everything) renders cleanly — no crashes, no NaN.
- Missing data degrades gracefully — segments omit instead of crashing.
- ``NO_COLOR`` env var honoured — emits empty style strings when set.

The render function is pulled out of ``input_loop.py`` so unit tests can
exercise the formatter without spinning up prompt_toolkit.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Public so callers / tests can reference these without importing magic
# constants. Defaults match the Claude Code visual.
#
# 2026-05-12 redesign — swapped the U+2695 caduceus (rendered as ``$`` in
# Consolas / Cascadia Mono on Windows where the medical glyph is absent)
# for the U+25C6 black diamond which ships in every default Windows
# Terminal font. Separator dropped from heavy U+2502 vertical bar to a
# breezier middle dot.
PREFIX = "◆ "  # U+25C6 black diamond + space
SEPARATOR = "  ·  "  # U+00B7 middle dot, double-space pads
BAR_FILL = "█"  # U+2588 (full block)
BAR_EMPTY = "░"  # U+2591 (light shade)
BAR_WIDTH = 10

#: Mid-truncation cap for the model id segment. Anything longer than this
#: gets squeezed to ``head…tail`` so the bar doesn't wrap on narrow
#: terminals or long OpenRouter slugs (``minimax/minimax-m2.5:free`` →
#: ``minimax/min…2.5:free``).
MODEL_ID_DISPLAY_MAX = 28

#: Conservative default when a model id has no entry in
#: ``DEFAULT_CONTEXT_WINDOWS`` and no ``-1m`` hint. 200k matches the
#: dominant Claude / OpenAI o-series window.
DEFAULT_MAX_CONTEXT = 200_000

#: Extended-context override triggered by ``-1m`` / ``1m`` in the model id.
EXTENDED_MAX_CONTEXT = 1_000_000


def _no_color() -> bool:
    """Honour the ``NO_COLOR`` env var. Empty/unset → colors enabled."""
    return bool(os.environ.get("NO_COLOR"))


def _style(spec: str) -> str:
    """Return ``spec`` or ``""`` depending on ``NO_COLOR``.

    Status-line styles are purely cosmetic — under ``NO_COLOR`` we emit
    plain text fragments and prompt_toolkit renders them without ANSI
    escapes. Nothing depends on the styling for correctness.
    """
    return "" if _no_color() else spec


def format_tokens(n: int) -> str:
    """Format a token count using K-suffix.

    - ``999`` → ``"999"``
    - ``1_000`` → ``"1.0K"``
    - ``12_400`` → ``"12.4K"``
    - ``200_000`` → ``"200K"`` (whole-K shown without decimal)
    - ``1_000_000`` → ``"1.0M"``
    - ``1_500_000`` → ``"1.5M"``

    Negative / non-int / NaN-y inputs collapse to ``"0"``.
    """
    if not isinstance(n, int) or n < 0:
        return "0"
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{m:.0f}M" if m == int(m) else f"{m:.1f}M"
    if n >= 1_000:
        k = n / 1_000
        # Whole-K without decimals — matches Claude Code's ``200K`` style.
        return f"{k:.0f}K" if k == int(k) else f"{k:.1f}K"
    return str(n)


def format_cost(cost: float | int | None) -> str:
    """Format a USD figure as ``$X.YY``.

    - ``None`` / non-numeric → ``""`` (caller omits the segment)
    - ``0`` → ``"$0.00"``
    - ``0.06`` → ``"$0.06"``
    - ``12.345`` → ``"$12.35"`` (banker-style rounding via :func:`round`)

    Negative is treated as zero — an LLM call cost can't be negative
    and a stray sign would look like a refund.
    """
    if cost is None:
        return ""
    if not isinstance(cost, (int, float)):
        return ""
    c = max(float(cost), 0.0)
    return f"${c:.2f}"


def format_elapsed(seconds: float | int) -> str:
    """Format an elapsed-time delta in seconds.

    Buckets:

    - ``< 60``     → ``"<n>s"`` (integer seconds)
    - ``< 3600``   → ``"<n>m"`` (integer minutes; floor)
    - ``>= 3600``  → ``"<H>h<M>m"`` (e.g. ``"1h23m"``)

    Negative / non-finite → ``"0s"`` (clock skew protection).
    """
    try:
        s = int(max(float(seconds), 0.0))
    except (TypeError, ValueError):
        return "0s"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    hours = s // 3600
    minutes = (s % 3600) // 60
    return f"{hours}h{minutes}m"


def progress_bar(used: int, total: int, width: int = BAR_WIDTH) -> str:
    """Render a fixed-width progress bar.

    - ``used <= 0`` → all empty cells
    - ``used >= total`` → all filled cells
    - else → proportional fill rounded down + remainder empty

    The width is fixed at :data:`BAR_WIDTH` so the status line stays
    visually stable across model swaps.
    """
    if width <= 0:
        return ""
    if total <= 0 or used <= 0:
        return BAR_EMPTY * width
    ratio = min(used / total, 1.0)
    filled = max(1, int(ratio * width))
    return BAR_FILL * filled + BAR_EMPTY * (width - filled)


def bar_color_for(percent: int) -> str:
    """Return a prompt_toolkit color spec graded by % context used.

    Tiers chosen to mirror common terminal-app heat scales:
      - 0-49%   → green   ("plenty of headroom")
      - 50-74%  → yellow  ("getting full")
      - 75-89%  → orange  ("compact soon")
      - 90%+    → red     ("compact imminent")

    Returns a bare color spec (no ``fg:`` prefix) so callers can compose
    it with bold/italic attributes if they want.
    """
    if percent >= 90:
        return "fg:ansired bold"
    if percent >= 75:
        return "fg:#ff8700"  # orange (256-color)
    if percent >= 50:
        return "fg:ansiyellow"
    return "fg:ansigreen"


def truncate_model_id(model_id: str, *, max_len: int = MODEL_ID_DISPLAY_MAX) -> str:
    """Mid-truncate a long model id so it fits on one status-line row.

    Preserves the head + tail so the user can still recognise the model
    family AND the variant tag:

        ``minimax/minimax-m2.5:free`` (24) → unchanged
        ``some-very-long-vendor/model-codename-v2.5:free`` → ``some-very-long…2.5:free``

    Empty / short ids pass through verbatim.
    """
    if not isinstance(model_id, str):
        return ""
    if len(model_id) <= max_len:
        return model_id
    # 1-char ellipsis, split remaining budget head-heavy so the prefix
    # (which carries the vendor / family) stays readable.
    budget = max_len - 1
    head_len = (budget * 2) // 3
    tail_len = budget - head_len
    return f"{model_id[:head_len]}…{model_id[-tail_len:]}"


def percent_used(used: int, total: int) -> int:
    """Integer-floor percentage. ``0`` when ``total`` is non-positive."""
    if total <= 0 or used <= 0:
        return 0
    pct = int((used / total) * 100)
    # Cap at 100 — we don't want ``120%`` showing on a slight overflow.
    return min(pct, 100)


def format_percent_used(used: int, total: int) -> str:
    """Render context usage with a visible low-usage state."""
    if total <= 0 or used <= 0:
        return "0%"
    pct = percent_used(used, total)
    return "<1%" if pct == 0 else f"{pct}%"


def max_context_for(
    model_id: str,
    *,
    model_context_overrides: dict | None = None,
    custom_providers: tuple = (),
) -> int:
    """Resolve max-context-window for a model id.

    Resolution order (highest → lowest priority):
      1. ``-1m`` / ``-1M`` suffix in the id → :data:`EXTENDED_MAX_CONTEXT`.
         This wins because vendor aliases like ``claude-sonnet-4-6-1m``
         aren't in the table and we don't want the conservative default
         to clip the bar.
      2. ``model_context_overrides`` (Wave 3) — flat user-supplied
         per-model overrides from ``config.yaml`` so a stale embedded
         table doesn't show ``475/200K`` when the actual window is 1M.
      3. ``custom_providers[].models[<id>].context_length`` (Wave 3) —
         scoped per-named-endpoint override.
      4. ``opencomputer.agent.compaction.context_window_with_overrides`` —
         honors all of (2) + (3) + the canonical static table.
      5. :data:`DEFAULT_MAX_CONTEXT` as the final fallback.

    Cold-path import: kept inside the function so module load doesn't
    pull the compaction engine into every CLI render.
    """
    if not isinstance(model_id, str) or not model_id:
        return DEFAULT_MAX_CONTEXT
    lowered = model_id.lower()
    # Explicit ``-1m`` / ``-1m-`` / trailing ``1m`` segment indicates the
    # vendor's million-token variant. Substring match covers both
    # ``claude-sonnet-4-6-1m`` and the ``[1m]`` Hermes alias suffix some
    # callers stash in the model id.
    if "-1m" in lowered or lowered.endswith("1m") or "[1m]" in lowered:
        return EXTENDED_MAX_CONTEXT
    try:
        from opencomputer.agent.compaction import context_window_with_overrides

        # Hot path — every keystroke triggers a render. ``enable_probe=
        # False`` keeps this synchronous-fast; the agent loop's first
        # turn fills the probe cache via the same resolver, after
        # which subsequent renders return the up-to-date value with
        # zero network latency.
        return int(
            context_window_with_overrides(
                model_id,
                custom_providers=custom_providers,
                model_context_overrides=model_context_overrides,
                enable_probe=False,
            )
        )
    except Exception:  # noqa: BLE001 — fallback path
        return DEFAULT_MAX_CONTEXT


def _read_runtime_state(runtime: object) -> dict[str, Any]:
    """Pluck the status-line keys out of ``runtime.custom`` defensively.

    Returns a dict with normalised values:

    - ``model_id`` (str)
    - ``tokens_used`` (int)
    - ``cost`` (float | None)
    - ``started_at`` (float | None — monotonic seconds)
    """
    if runtime is None:
        return {
            "model_id": "",
            "tokens_used": 0,
            "cost": None,
            "started_at": None,
            "model_context_overrides": {},
            "custom_providers": (),
        }
    custom = getattr(runtime, "custom", None) or {}
    model_id = (
        custom.get("model_id")
        or custom.get("active_model_id")
        or custom.get("model")
        or ""
    )
    if not isinstance(model_id, str):
        model_id = str(model_id)

    # 2026-05-11: the bar reports the CURRENT request size, NEVER the
    # cumulative ``session_tokens_in + session_tokens_out``. Cumulative
    # input inflates by ~10x after 10 turns (each turn re-sends the
    # full history → cumulative input grows linearly with turn count
    # while the *actual* current request stays bounded). Summing in
    # output is double-counting — every output token re-enters the
    # next turn's input and is already in that turn's
    # ``last_input_tokens``.
    #
    # ``resolve_current_input_tokens`` is the single resolver shared
    # with the ``/context`` slash command — same source of truth for
    # both surfaces, so they can never drift again.
    from opencomputer.agent.compaction import (
        resolve_current_input_tokens,
    )

    tokens_used = resolve_current_input_tokens(custom)

    cost = custom.get("session_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) else None

    started = custom.get("session_started_at")
    started = float(started) if isinstance(started, (int, float)) else None

    overrides = custom.get("model_context_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    cps = custom.get("custom_providers") or ()
    if not isinstance(cps, tuple):
        cps = tuple(cps) if isinstance(cps, (list, tuple)) else ()
    return {
        "model_id": model_id,
        "tokens_used": tokens_used,
        "cost": cost,
        "started_at": started,
        "model_context_overrides": overrides,
        "custom_providers": cps,
    }


def _now_monotonic() -> float:
    """Indirection so tests can monkeypatch elapsed-time reads."""
    return time.monotonic()


def render_status_line(runtime: object) -> list[tuple[str, str]]:
    """Return prompt_toolkit FormattedText fragments for the status line.

    Always returns at least the prefix + model + tokens + bar + percent
    segments. Cost and elapsed segments are appended when their backing
    state is present; otherwise the segment is omitted (no crash, no
    placeholder text).

    The render must remain O(1) — the agent loop is responsible for
    keeping ``runtime.custom`` populated; we only read.
    """
    state = _read_runtime_state(runtime)
    model_id = state["model_id"]
    tokens_used = state["tokens_used"]
    cost = state["cost"]
    started = state["started_at"]

    max_ctx = max_context_for(
        model_id,
        model_context_overrides=state.get("model_context_overrides") or {},
        custom_providers=state.get("custom_providers") or (),
    )
    bar = progress_bar(tokens_used, max_ctx)
    pct_int = percent_used(tokens_used, max_ctx)

    style_prefix = _style("fg:ansicyan bold")
    style_model = _style("fg:ansiwhite bold")
    style_sep = _style("fg:ansibrightblack")
    style_tokens_label = _style("fg:ansibrightblack")
    style_tokens = _style("")
    style_bar = _style(bar_color_for(pct_int))
    style_pct = _style("bold" if pct_int >= 75 else "")
    style_cost = _style("fg:ansiyellow")
    style_time = _style("fg:ansibrightblack")

    fragments: list[tuple[str, str]] = []
    fragments.append((style_prefix, " " + PREFIX))
    fragments.append((style_model, truncate_model_id(model_id) or "default"))
    fragments.append((style_sep, SEPARATOR))
    fragments.append((style_tokens_label, "ctx "))
    fragments.append((
        style_tokens,
        f"{format_tokens(tokens_used)}/{format_tokens(max_ctx)}",
    ))
    fragments.append((style_sep, " "))
    fragments.append((style_bar, bar))
    fragments.append((style_pct, f" {format_percent_used(tokens_used, max_ctx)}"))

    cost_str = format_cost(cost)
    if cost_str:
        fragments.append((style_sep, SEPARATOR))
        fragments.append((style_cost, cost_str))

    if started is not None:
        fragments.append((style_sep, SEPARATOR))
        fragments.append((
            style_time,
            format_elapsed(_now_monotonic() - started),
        ))
    else:
        # Cold-start: still show ``0s`` so the user sees the field exists.
        fragments.append((style_sep, SEPARATOR))
        fragments.append((style_time, "0s"))

    # Hermes-CLI parity A3 — per-prompt elapsed clock. Reads from
    # ``runtime.custom["_prompt_clock"]`` if present (PromptClock instance).
    # Renders empty when no prompt is in flight.
    custom = getattr(runtime, "custom", None) or {}
    clock = custom.get("_prompt_clock")
    if clock is not None:
        try:
            tag = clock.render()
        except Exception:  # noqa: BLE001 — never crash the status line
            tag = ""
        if tag:
            fragments.append((style_sep, SEPARATOR))
            fragments.append((style_time, tag))

    fragments.append(("", " "))  # trailing pad so the right edge breathes
    return fragments


__all__ = [
    "BAR_EMPTY",
    "BAR_FILL",
    "BAR_WIDTH",
    "DEFAULT_MAX_CONTEXT",
    "EXTENDED_MAX_CONTEXT",
    "MODEL_ID_DISPLAY_MAX",
    "PREFIX",
    "SEPARATOR",
    "bar_color_for",
    "format_cost",
    "format_elapsed",
    "format_percent_used",
    "format_tokens",
    "max_context_for",
    "percent_used",
    "progress_bar",
    "render_status_line",
    "truncate_model_id",
]
