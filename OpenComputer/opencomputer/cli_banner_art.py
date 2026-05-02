"""ASCII art constants for the welcome banner.

Visual register modeled after hermes-agent's banner.py (HERMES-AGENT
art) — independently re-drawn (no glyphs copied). Logo uses figlet
'slant' font with tweaks; side glyph is a simple geometric mark.
"""
from __future__ import annotations

# Plain-text fallback used when terminal width can't accommodate the
# figlet-style rendering. Also satisfies the test that asserts
# "OPENCOMPUTER" appears somewhere in the art module.
OPENCOMPUTER_LOGO_FALLBACK = "OPENCOMPUTER"

# Logo: figlet 'slant'-style rendering of "OPENCOMPUTER"
# Hand-tuned so OPENCOMPUTER reads cleanly even on dark terminals.
OPENCOMPUTER_LOGO = r"""
   ____  ____  _________   __ _________________  __  __  ____________  ____
  / __ \/ __ \/ ____/ __ \ / // ____/ __ \/  |/  / __ \/ / / /_  __/ ____/ __ \
 / / / / /_/ / __/ / / / // // /   / / / / /|_/ / /_/ / / / / / / / __/ / /_/ /
/ /_/ / ____/ /___/ /| | // // /___/ /_/ / /  / / ____/ /_/ / / / / /___/ _, _/
\____/_/   /_____/_/ |_|//_(_)____/\____/_/  /_/_/    \____/ /_/ /_____/_/ |_|
"""

# Side glyph: braille-pattern geometric mark inspired by Hermes's caduceus
# (``hermes_cli/banner.py:HERMES_CADUCEUS``). Independently re-drawn so it
# represents OpenComputer's identity (the "OC" double-circle) rather than
# Hermes's specific symbol. Uses braille block characters so it renders
# crisply at small sizes — the previous ASCII-dot version had visible
# alignment seams on most terminals.
SIDE_GLYPH = r"""
            ⢀⣤⣶⣶⣦⣄⡀
        ⢀⣴⣿⠟⠉  ⠉⠻⣿⣦⡀
      ⣰⣿⠟⠁  ⢀⣀⣀  ⠈⠻⣿⣆
    ⣴⡿⠋  ⢀⣴⣿⠟⠛⢿⣷⣦⡀  ⠙⢿⣦
   ⣿⠏    ⣾⡟⠁    ⠈⢻⣷    ⠹⣿
  ⣸⡿     ⣿⡇  OC   ⢸⣿     ⢿⣇
   ⣿⡆    ⢿⣧⡀    ⢀⣼⡿    ⢰⣿
    ⠹⣿⣄  ⠈⠻⢿⣷⣶⣶⡿⠟⠁  ⣠⣿⠏
      ⠙⣿⣆⡀  ⠉⠉⠉⠉  ⢀⣰⣿⠋
        ⠙⢿⣷⣄⡀    ⢀⣠⣾⡿⠋
            ⠉⠛⠿⠿⠟⠋
"""
