"""Shell command that the user runs to launch their existing Chrome
with ``--remote-debugging-port=9222`` against their normal profile.

Used by ``oc browser chrome`` (CLI) for the existing-session profile
flow — auto-launching the user's browser is intentionally OUT of scope;
they own that decision.

Ported from the legacy top-level ``chrome_launch.py`` (deleted in W3)
into the ``chrome/`` package so the CLI can find it next to the rest
of the Chrome process management code.
"""

from __future__ import annotations

import sys

CHROME_LAUNCH_COMMANDS: dict[str, str] = {
    "darwin": (
        '/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
        '--remote-debugging-port=9222 '
        '--user-data-dir="$HOME/Library/Application Support/Google/Chrome"'
    ),
    "linux": (
        'google-chrome --remote-debugging-port=9222 '
        '--user-data-dir="$HOME/.config/google-chrome"'
    ),
    "win32": (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port=9222 '
        '--user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\User Data"'
    ),
}


def chrome_launch_command(platform: str | None = None) -> str:
    """Return the shell command to launch Chrome with CDP debugging enabled.

    Reuses the user's existing Chrome profile so logins, cookies, and
    extensions are preserved when OpenComputer attaches via CDP.

    Raises ``NotImplementedError`` for platforms we haven't templated;
    the caller should fall back to a generic message instructing the
    user to add ``--remote-debugging-port=9222`` to their Chrome launch
    however they normally start it.
    """
    if platform is None:
        platform = sys.platform
    if platform not in CHROME_LAUNCH_COMMANDS:
        raise NotImplementedError(
            f"No Chrome launch command for platform {platform!r}. "
            "Pass --remote-debugging-port=9222 to chrome and set "
            "OPENCOMPUTER_BROWSER_CDP_URL=http://localhost:9222."
        )
    return CHROME_LAUNCH_COMMANDS[platform]


__all__ = ["CHROME_LAUNCH_COMMANDS", "chrome_launch_command"]
