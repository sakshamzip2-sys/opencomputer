"""ASCII art constants for the OpenComputer welcome banner.

The active splash (``cli_banner.build_welcome_banner``) renders the
Hermes-style ``ansi_shadow`` wordmarks — ``OPEN_COMPUTER_LOGO_HERMES_STYLE``
(one-line, wide terminals) or ``OPEN_COMPUTER_LOGO_HERMES_STACKED``
(stacked, narrow terminals). ``OPENCOMPUTER_BLOCK_LOGO``,
``OPENCOMPUTER_LOGO`` and ``SIDE_GLYPH`` are kept for backwards compat
with external consumers and test fixtures; the active splash no longer
renders them.
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

# Legacy half-block "Solid" wordmark of OPENCOMPUTER from the 2026-05-10
# banner-redesign. 71 cols × 3 rows, half-blocks (▀ ▄ █). Kept for
# back-compat with tests / external consumers; the active splash no
# longer renders it — narrow terminals now get the stacked Hermes-style
# wordmark (OPEN_COMPUTER_LOGO_HERMES_STACKED).
OPENCOMPUTER_BLOCK_LOGO = (
    "▄▀▀▀▄ █▀▀▀▄ █▀▀▀▀ █▄  █ ▄▀▀▀▀ ▄▀▀▀▄ █▄ ▄█ █▀▀▀▄ █   █ ▀▀█▀▀ █▀▀▀▀ █▀▀▀▄\n"
    "█   █ █▄▄▄▀ █▄▄   █ █ █ █     █   █ █ ▀ █ █▄▄▄▀ █   █   █   █▄▄   █▄▄▄▀\n"
    "▀▄▄▄▀ █     █▄▄▄▄ █  ▀█ ▀▄▄▄▄ ▀▄▄▄▀ █   █ █     ▀▄▄▄▀   █   █▄▄▄▄ █ ▀▄▄\n"
)


# 2026-05-12 Hermes-style splash — chunky 8-bit ``ansi_shadow`` wordmark
# of ``OPEN-COMPUTER`` (110 cols × 6 rows) hand-colored as a 3-tier pink
# gradient. Same technique upstream Hermes uses for its gold/amber/bronze
# ``HERMES_AGENT_LOGO`` (sources/hermes-agent/hermes_cli/banner.py); we
# swap colors to OC pink (#FF3D8A → #E91E78 → #C2185B). Generated via
# ``pyfiglet.figlet_format("OPEN-COMPUTER", font="ansi_shadow")`` and
# colored by hand so we don't pay the pyfiglet import at runtime.
OPEN_COMPUTER_LOGO_HERMES_STYLE = """[bold #FF3D8A] ██████╗ ██████╗ ███████╗███╗   ██╗       ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗██████╗ [/]
[bold #FF3D8A]██╔═══██╗██╔══██╗██╔════╝████╗  ██║      ██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗[/]
[#E91E78]██║   ██║██████╔╝█████╗  ██╔██╗ ██║█████╗██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗  ██████╔╝[/]
[#E91E78]██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║╚════╝██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝  ██╔══██╗[/]
[#C2185B]╚██████╔╝██║     ███████╗██║ ╚████║      ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗██║  ██║[/]
[#C2185B] ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝       ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝[/]"""

OPEN_COMPUTER_LOGO_HERMES_STYLE_WIDTH = 110  # widest row, cell-counted


# 2026-05-15 — stacked Hermes-style wordmark for terminals too narrow
# for the 110-col one-line OPEN_COMPUTER_LOGO_HERMES_STYLE. Same chunky
# ``ansi_shadow`` figlet font, "OPEN" stacked over "COMPUTER" (70 cols ×
# 12 rows), hand-colored with the same 3-tier pink gradient per word.
# Generated via pyfiglet.figlet_format("OPEN" | "COMPUTER",
# font="ansi_shadow").
OPEN_COMPUTER_LOGO_HERMES_STACKED = """[bold #FF3D8A] ██████╗ ██████╗ ███████╗███╗   ██╗[/]
[bold #FF3D8A]██╔═══██╗██╔══██╗██╔════╝████╗  ██║[/]
[#E91E78]██║   ██║██████╔╝█████╗  ██╔██╗ ██║[/]
[#E91E78]██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║[/]
[#C2185B]╚██████╔╝██║     ███████╗██║ ╚████║[/]
[#C2185B] ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝[/]
[bold #FF3D8A] ██████╗ ██████╗ ███╗   ███╗██████╗ ██╗   ██╗████████╗███████╗██████╗ [/]
[bold #FF3D8A]██╔════╝██╔═══██╗████╗ ████║██╔══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗[/]
[#E91E78]██║     ██║   ██║██╔████╔██║██████╔╝██║   ██║   ██║   █████╗  ██████╔╝[/]
[#E91E78]██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██║   ██║   ██║   ██╔══╝  ██╔══██╗[/]
[#C2185B]╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ╚██████╔╝   ██║   ███████╗██║  ██║[/]
[#C2185B] ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝      ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝[/]"""

OPEN_COMPUTER_LOGO_HERMES_STACKED_WIDTH = 70  # widest row ("COMPUTER")


# Laurels Braille-art (user-supplied, generated via laurels.py from the
# game-icons/lorc/laurels SVG — symmetric wreath silhouette). 16 rows
# tall, 31 cells wide at peak, painted with a 5-tier pink gradient:
# dark-rose top tips → rose → hot-pink body → rose → dark-rose → very
# dark tail fade. Constant name kept as ``OPEN_COMPUTER_CADUCEUS_PINK``
# for back-compat with ``cli_banner`` imports (it's the left-column
# hero slot — pegasus before, laurels now, name is just a slot ID).
OPEN_COMPUTER_CADUCEUS_PINK = """[#C2185B]          ⢀⣀⣠⣤⡄  ⢠⣤⣄⣀⡀[/]
[#C2185B]       ⢀⣤⣠⣿⣿⡿⠋    ⠙⢿⣿⣿⣄⣤⡀[/]
[#E91E78]      ⠰⣿⣿⣿⣿⠁        ⠈⣿⣿⣿⣿⠆[/]
[#E91E78]    ⣴⣿⣶⣿⡿⠿⠃          ⠘⠿⢿⣿⣶⣿⣦[/]
[#E91E78]   ⠸⣿⣿⣿⣿⣷⠄            ⠠⣾⣿⣿⣿⣿⠇[/]
[#FF3D8A]  ⣴⣦⣨⣿⡿⠿⠟              ⠻⠿⢿⣿⣅⣴⣦[/]
[#FF3D8A] ⢰⣿⣿⣿⣿⣷⣶⡄              ⢠⣶⣾⣿⣿⣿⣿⡆[/]
[#FF3D8A] ⠈⠛⠿⣿⣿⡿⠿⠃              ⠘⠿⢿⣿⣿⠿⠛⠁[/]
[#FF3D8A] ⢸⣿⣶⣿⣿⣶⣶⣦              ⣴⣶⣶⣿⣿⣶⣿⡇[/]
[#FF3D8A] ⠸⣿⣿⣿⣿⣿⣿⠟              ⠻⣿⣿⣿⣿⣿⣿⠇[/]
[#E91E78]  ⢈⣉⣉⣿⣿⣤⣴⣿⣦          ⣴⣿⣦⣤⣿⣿⣉⣉⡁[/]
[#E91E78]  ⠹⣿⣿⣿⣿⣿⣿⣿⡿⢀⣤⡀    ⢀⣤⡀⢿⣿⣿⣿⣿⣿⣿⣿⠏[/]
[#E91E78]   ⠹⣿⣿⡿⢿⣿⣏⣴⣿⣿⣿    ⣿⣿⣿⣦⣹⣿⡿⢿⣿⣿⠏[/]
[#C2185B]     ⢠⣴⣾⣿⣿⣿⣿⡿⠃    ⠘⢿⣿⣿⣿⣿⣷⣦⡄[/]
[#C2185B]      ⠙⢿⣿⠿⠛⠻⣷⣤⡀  ⢀⣤⣾⠟⠛⠿⣿⡿⠋[/]
[#8E1A4F]             ⠙⠁  ⠈⠋[/]"""

OPEN_COMPUTER_CADUCEUS_PINK_WIDTH = 31  # widest source row (rows 5–9)
