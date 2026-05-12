"""Locate the hermes-workspace directory on disk.

Mirrors ``oc webui``'s search order (see ``opencomputer.cli:webui``) so
operators familiar with one command find the other intuitive.

Search order:

1. Explicit ``workspace_dir`` argument (from ``--workspace-dir`` CLI flag)
2. ``$OC_WORKSPACE_DIR`` env var
3. ``<profile_home>/workspace/``
4. ``~/.opencomputer/workspace/``
5. ``/Users/saksham/Vscode/claude/sources/hermes-workspace/`` (dev-only
   sibling; included for the author's local setup where the source repo
   already lives at that path)
6. Fail loud with the searched paths
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DEFAULT_DEV_SOURCES_PATH",
    "WorkspaceNotFoundError",
    "discover_workspace_dir",
    "is_valid_workspace_dir",
]


#: Dev fallback path. Present on the author's machine; missing on most
#: deployments. Discovery treats a missing path as "skip, continue to next
#: candidate" — never as an error.
DEFAULT_DEV_SOURCES_PATH = Path(
    "/Users/saksham/Vscode/claude/sources/hermes-workspace"
)


class WorkspaceNotFoundError(RuntimeError):
    """Raised when no valid hermes-workspace dir can be located.

    ``searched`` lists every candidate path that was checked, in priority
    order, so the CLI can surface a precise error message.
    """

    def __init__(self, searched: list[Path]) -> None:
        self.searched = searched
        joined = "\n  ".join(str(p) for p in searched)
        super().__init__(
            "hermes-workspace directory not found. Searched:\n  " + joined
        )


def is_valid_workspace_dir(path: Path) -> bool:
    """Return True iff ``path`` looks like a hermes-workspace checkout.

    The signal we use is the combination of ``package.json`` AND
    ``server-entry.js`` — both ship in the upstream repo and neither
    appears together in random Node projects. We deliberately do NOT
    require ``dist/`` to exist; ``oc workspace build`` produces that on
    first run.
    """
    if not isinstance(path, Path):
        return False
    if not path.is_dir():
        return False
    pkg = path / "package.json"
    entry = path / "server-entry.js"
    return pkg.is_file() and entry.is_file()


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def discover_workspace_dir(
    *,
    explicit: str | Path | None = None,
    profile_home: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the hermes-workspace directory or raise.

    Args:
        explicit: ``--workspace-dir`` value. If non-None and invalid, raises
            (we do NOT silently fall through to the next candidate — a typo
            would otherwise launch the wrong workspace under the user's nose).
        profile_home: Active profile home. Used to build candidate #3.
        env: Environment to read ``OC_WORKSPACE_DIR`` from. Defaults to
            ``os.environ``. Injectable for tests.

    Raises:
        WorkspaceNotFoundError: when neither explicit nor any fallback
            candidate resolves to a valid workspace dir.
        ValueError: when the explicit path is non-empty but invalid.
    """
    if env is None:
        env = dict(os.environ)

    if explicit is not None and str(explicit).strip():
        path = _resolve(Path(str(explicit)))
        if not is_valid_workspace_dir(path):
            raise ValueError(
                f"--workspace-dir {path} is not a valid hermes-workspace "
                "(missing package.json or server-entry.js)"
            )
        return path

    env_val = (env.get("OC_WORKSPACE_DIR") or "").strip()
    if env_val:
        path = _resolve(Path(env_val))
        if not is_valid_workspace_dir(path):
            raise ValueError(
                f"$OC_WORKSPACE_DIR={path} is not a valid hermes-workspace "
                "(missing package.json or server-entry.js)"
            )
        return path

    candidates: list[Path] = []
    if profile_home is not None:
        candidates.append(profile_home / "workspace")
    candidates.append(Path.home() / ".opencomputer" / "workspace")
    candidates.append(DEFAULT_DEV_SOURCES_PATH)

    for cand in candidates:
        resolved = _resolve(cand)
        if is_valid_workspace_dir(resolved):
            return resolved

    raise WorkspaceNotFoundError([_resolve(c) for c in candidates])
