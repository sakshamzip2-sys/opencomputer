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
