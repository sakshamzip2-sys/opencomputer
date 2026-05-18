"""Busy-indicator glyph styles for the CLI status line.

Hermes-CLI parity (doc lines 329-336). Five named styles each have a
uniform display-width invariant — every frame in a style is the same
``wcwidth`` so the status bar doesn't jitter on rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    import wcwidth
except ImportError:  # pragma: no cover — wcwidth is a hard dep elsewhere
    wcwidth = None  # type: ignore[assignment]


def _wcswidth(s: str) -> int:
    """Width-safe wrapper — clamps unprintable to 0 (wcwidth returns -1)."""
    if wcwidth is None:
        # crude fallback: count chars (works for ASCII-only)
        return max(len(s), 0)
    w = wcwidth.wcswidth(s)
    return max(w if w is not None else 0, 0)


def _pad_to_uniform(frames: tuple[str, ...]) -> tuple[str, ...]:
    """Right-pad every frame with U+0020 so they all have the same width."""
    widths = [_wcswidth(f) for f in frames]
    target = max(widths) if widths else 0
    return tuple(f + " " * (target - w) for f, w in zip(frames, widths, strict=True))


STYLES: dict[str, tuple[str, ...]] = {
    "kawaii": _pad_to_uniform((
        "(•́︿•̀)",
        "(⊙_⊙)",
        "( ˘ω˘)",
        "(づ◕‿◕)づ",
    )),
    "minimal": _pad_to_uniform(("⋯", "···", "·")),
    "dots": _pad_to_uniform(
        ("⠁", "⠃", "⠇", "⠧", "⠷", "⠿", "⠟", "⠏")
    ),
    "wings": _pad_to_uniform(("≼", "≼", "≽", "≽")),
    "none": ("",),
}


@dataclass
class BusyIndicator:
    """Cycle through the frames of a chosen style.

    Construction with an unknown style falls back to ``kawaii``.
    """

    style: str = "kawaii"
    _idx: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.style not in STYLES:
            self.style = "kawaii"

    def next_frame(self) -> str:
        frames = STYLES[self.style]
        f = frames[self._idx % len(frames)]
        self._idx += 1
        return f

    def reset(self) -> None:
        self._idx = 0


# ── /indicator face override (best-of-three Recipe 7) ────────────────
#
# The streaming spinner normally uses the active skin's faces
# (``streaming._skin_spinner_text``). ``/indicator <style>`` lets the
# user override just the face — independent of the skin — picking one
# of the STYLES above. ``minimal`` / ``none`` are the "spinner fatigue"
# escape hatches. Session-scoped: a module global, not persisted.

_INDICATOR_OVERRIDE: str = ""  # "" = no override (use the skin's faces)


def set_indicator_style(name: str) -> bool:
    """Set the busy-indicator face override.

    ``""`` / ``"skin"`` / ``"default"`` clear the override (back to the
    skin's faces). A known STYLES key sets it. Returns ``True`` when the
    value was accepted, ``False`` for an unknown style.
    """
    global _INDICATOR_OVERRIDE
    n = (name or "").strip().lower()
    if n in ("", "skin", "default"):
        _INDICATOR_OVERRIDE = ""
        return True
    if n in STYLES:
        _INDICATOR_OVERRIDE = n
        return True
    return False


def current_indicator_style() -> str:
    """Active override style, or ``""`` when none is set."""
    return _INDICATOR_OVERRIDE


def current_indicator_face() -> str:
    """First frame of the override style (padding stripped).

    Returns ``""`` when no override is set OR when the override is
    ``none`` — the caller then renders a verb-only spinner.
    """
    if not _INDICATOR_OVERRIDE:
        return ""
    frames = STYLES.get(_INDICATOR_OVERRIDE) or ("",)
    return frames[0].rstrip()
