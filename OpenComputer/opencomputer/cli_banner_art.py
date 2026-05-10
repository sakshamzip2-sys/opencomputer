"""ASCII art constants for the welcome banner.

Visual register modeled after hermes-agent's banner.py (HERMES-AGENT
art) — independently re-drawn (no glyphs copied). Original logo uses
figlet 'slant' font; the active banner uses ``OPENCOMPUTER_BLOCK_LOGO``
(ANSI-Shadow figlet, stacked OPEN/COMPUTER) for a chunkier presence.
"""
from __future__ import annotations

# Plain-text fallback used when terminal width can't accommodate the
# figlet-style rendering. Also satisfies the test that asserts
# "OPENCOMPUTER" appears somewhere in the art module.
OPENCOMPUTER_LOGO_FALLBACK = "OPENCOMPUTER"

# Legacy slant-style logo — kept for backwards compat with existing
# tests / external consumers; the active banner no longer renders it.
OPENCOMPUTER_LOGO = r"""
   ____  ____  _________   __ _________________  __  __  ____________  ____
  / __ \/ __ \/ ____/ __ \ / // ____/ __ \/  |/  / __ \/ / / /_  __/ ____/ __ \
 / / / / /_/ / __/ / / / // // /   / / / / /|_/ / /_/ / / / / / / / __/ / /_/ /
/ /_/ / ____/ /___/ /| | // // /___/ /_/ / /  / / ____/ /_/ / / / / /___/ _, _/
\____/_/   /_____/_/ |_|//_(_)____/\____/_/  /_/_/    \____/ /_/ /_____/_/ |_|
"""

# Side glyph: legacy geometric mark with "OC" in the center — kept for
# back-compat. The active banner no longer renders it.
SIDE_GLYPH = r"""
        .::::::.
      .::::::::::.
     :::: OC ::::
    :::::::::::::::
   :::      :::::
   :::      :::::
   :::      :::::
    :::::::::::::::
     :::::::::::
      .::::::::::.
        .::::::.
"""

# Active logo: single-line ANSI-Shadow figlet of OPENCOMPUTER. 105
# cols × 6 lines tall. Hermes-screenshot parity — chunky title that
# reads as a single word. Narrow terminals fall back to a one-line
# inline title (handled in cli_banner.build_welcome_banner).
OPENCOMPUTER_BLOCK_LOGO = (
    " ██████╗ ██████╗ ███████╗███╗   ██╗ ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗██████╗ \n"
    "██╔═══██╗██╔══██╗██╔════╝████╗  ██║██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗\n"
    "██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗  ██████╔╝\n"
    "██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝  ██╔══██╗\n"
    "╚██████╔╝██║     ███████╗██║ ╚████║╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗██║  ██║\n"
    " ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝\n"
)
