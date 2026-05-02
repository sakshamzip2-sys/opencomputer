"""OS-specific Chrome launch command for CDP attach mode.

Returns the shell command the user runs to launch Chrome with
``--remote-debugging-port=9222`` against their existing Chrome profile,
so logins / cookies / extensions are preserved when OpenComputer
attaches via CDP.

We deliberately do NOT auto-launch Chrome — it's the user's browser,
their choice. The 'oc browser chrome' CLI prints the command for the
user to copy and run in a separate terminal.
"""

from __future__ import annotations

import sys

CHROME_LAUNCH_COMMANDS = {
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

    Uses the user's existing Chrome profile so logins, cookies, and
    extensions are preserved.

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
