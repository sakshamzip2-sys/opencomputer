"""Cross-platform move-to-trash via send2trash.

OpenClaw's trash.ts hand-rolled a Linux fallback that puts files under
~/.Trash, which is wrong (XDG says ~/.local/share/Trash/files). We use
send2trash exclusively; it handles macOS Finder, Linux gvfs/trash-cli,
and Windows recycle bin correctly.

The send2trash import is intentionally lazy (inside the function) so this
module can be imported without the [browser] extra installed. Without
this, conftest.py's eager `_register_browser_control_alias()` would crash
every CI matrix that doesn't install [browser] (e.g. introspection
cross-platform), even though those tests don't touch browser code.
"""

from __future__ import annotations

import os


def move_to_trash(path: str | os.PathLike[str]) -> None:
    from send2trash import (
        send2trash,  # lazy: keeps this module import-clean without [browser] extras
    )

    send2trash(os.fspath(path))
