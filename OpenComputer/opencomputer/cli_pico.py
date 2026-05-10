"""Pico — the Open Computer mascot.

Pico is an 8-bit pill bug who lives in the keyboard. Designed in the
Claude Design bundle (`oc-mascot/project/Open Computer Mascots.html`,
final iteration `mascots.js`). The grids and color tokens here are
ports of `PICO.grid` and the `pico*()` mutation functions from the
JS source — kept verbatim so the terminal silhouette matches the
SVG pixel-for-pixel.

Rendering uses Unicode upper/lower half-blocks so each terminal cell
encodes two stacked pixels. This keeps the silhouette square (one
char ≈ two pixels tall) without leaning on truecolor backgrounds.
"""
from __future__ import annotations

from rich.text import Text

__all__ = [
    "PICO_GRID",
    "PICO_EXPRESSIONS",
    "render_pico",
]

ROSE = "#C2185B"
ROSE_LIGHT = "#E91E78"
ROSE_DEEP = "#AD1457"

PICO_GRID: list[str] = [
    "................",
    "......####......",
    "....########....",
    "..############..",
    ".###..####..###.",
    ".##############.",
    ".##############.",
    "..############..",
    "....########....",
    "....##....##....",
    "....##....##....",
]


def _pico_blink_grid() -> list[str]:
    g = list(PICO_GRID)
    g[4] = ".##############."
    return g


def _pico_happy_grid() -> list[str]:
    g = list(PICO_GRID)
    g[4] = ".###.######.###."
    return g


def _pico_curious_grid() -> list[str]:
    g = list(PICO_GRID)
    g[4] = ".##...####...##."
    return g


def _pico_rolled_grid() -> list[str]:
    return [
        "................",
        "................",
        "......####......",
        "....########....",
        "...##########...",
        "..############..",
        "..####....####..",
        "..############..",
        "...##########...",
        "....########....",
        "......####......",
    ]


def _pico_zoom_grid() -> list[str]:
    return [
        "................",
        "......####......",
        "....########....",
        "#.############..",
        "###..####..###..",
        "##############..",
        "#.############..",
        "...##########...",
        "....########....",
        "...##......##...",
        "..##........##..",
    ]


PICO_EXPRESSIONS: dict[str, tuple[list[str], str]] = {
    "idle":    (PICO_GRID,             ROSE),
    "blink":   (_pico_blink_grid(),    ROSE),
    "happy":   (_pico_happy_grid(),    ROSE),
    "curious": (_pico_curious_grid(),  ROSE),
    "rolled":  (_pico_rolled_grid(),   ROSE_DEEP),
    "zooming": (_pico_zoom_grid(),     ROSE_LIGHT),
}


def render_pico(expression: str = "idle") -> Text:
    """Render Pico in the requested expression as a Rich ``Text``.

    Half-block encoding: each character cell is one pair of stacked
    pixels. ``▀`` = top filled, ``▄`` = bottom filled, ``█`` = both,
    space = neither.
    """
    grid, color = PICO_EXPRESSIONS[expression]
    h = len(grid)
    if h % 2:
        grid = grid + ["." * len(grid[0])]
        h += 1
    w = len(grid[0])

    out = Text()
    for y in range(0, h, 2):
        top = grid[y]
        bot = grid[y + 1]
        for x in range(w):
            t = top[x] == "#"
            b = bot[x] == "#"
            if t and b:
                out.append("█", style=color)
            elif t:
                out.append("▀", style=color)
            elif b:
                out.append("▄", style=color)
            else:
                out.append(" ")
        if y + 2 < h:
            out.append("\n")
    return out
