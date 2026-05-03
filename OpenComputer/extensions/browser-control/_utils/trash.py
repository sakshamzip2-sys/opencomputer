"""Cross-platform move-to-trash via send2trash.

OpenClaw's trash.ts hand-rolled a Linux fallback that puts files under
~/.Trash, which is wrong (XDG says ~/.local/share/Trash/files). We use
send2trash exclusively; it handles macOS Finder, Linux gvfs/trash-cli,
and Windows recycle bin correctly.
"""

from __future__ import annotations

import os

from send2trash import send2trash


def move_to_trash(path: str | os.PathLike[str]) -> None:
    send2trash(os.fspath(path))
