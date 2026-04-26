"""Layer 2 — Recent Context Scan.

One-shot ingestion of "what's happening this week" so the agent has
current context, not just identity. Sources:

* Files modified in user-allowed dirs (this module)
* Git log across detected repos (this module)
* Calendar events (next 7 days) — see ``calendar_reader.py``
* Browser history — see ``browser_history.py``

Outputs are :class:`RecentFileSummary` / :class:`GitCommitSummary` /
``CalendarEventSummary`` / ``BrowserVisitSummary`` records that the
orchestrator (Task 9) feeds into the F4 user-model graph.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

#: Filenames + extensions skipped by the recent-files walker. Belt-
#: and-suspenders alongside dotfile-skip — secrets that happen to
#: live in plain-named files don't get ingested into motifs.
_SKIP_EXTENSIONS = frozenset({".env", ".key", ".pem", ".p12", ".pgp", ".asc"})
_SKIP_NAMES = frozenset({".env", ".envrc", "id_rsa", "id_ed25519"})


@dataclass(frozen=True, slots=True)
class RecentFileSummary:
    """Metadata-only summary of a recently-modified file."""

    path: str
    mtime: float
    size_bytes: int


@dataclass(frozen=True, slots=True)
class GitCommitSummary:
    """One-line git commit summary."""

    repo_path: str
    sha: str
    timestamp: float
    subject: str
    author_email: str


def scan_recent_files(
    *,
    roots: list[Path],
    days: int = 7,
    max_files: int = 1000,
) -> list[RecentFileSummary]:
    """Walk ``roots`` and return files modified in the last ``days``.

    Skips dotfiles, symlinks, files in :data:`_SKIP_NAMES`, and files
    with extensions in :data:`_SKIP_EXTENSIONS`. Caps at ``max_files``
    to keep the scan time bounded.

    Uses ``os.walk`` with in-place ``dirnames[:]`` mutation to prune dotted
    directories (e.g. ``.git/``, ``.cache/``, ``.npm/``) at the source so
    they are never enumerated, avoiding the performance, privacy, and noise
    problems caused by walking into those trees.
    """
    cutoff = time.time() - (days * 24 * 3600)
    out: list[RecentFileSummary] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(str(root)):
                # Prune dotted directories in-place so os.walk never recurses
                # into them (e.g. .git/, .cache/, .npm/, .idea/).
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    if fname in _SKIP_NAMES:
                        continue
                    p = Path(dirpath) / fname
                    if p.suffix.lower() in _SKIP_EXTENSIONS:
                        continue
                    if p.is_symlink():
                        continue
                    try:
                        stat = p.stat()
                    except OSError:
                        continue
                    if stat.st_mtime < cutoff:
                        continue
                    out.append(
                        RecentFileSummary(
                            path=str(p.resolve()),
                            mtime=stat.st_mtime,
                            size_bytes=stat.st_size,
                        )
                    )
                    if len(out) >= max_files:
                        return out
        except (OSError, PermissionError):
            continue
    return out


def scan_git_log(
    *,
    repo_paths: list[Path],
    days: int = 7,
    max_per_repo: int = 200,
) -> list[GitCommitSummary]:
    """Run ``git log`` in each repo and return commits in the last ``days``."""
    if shutil.which("git") is None:
        return []
    since = f"{days}.days.ago"
    out: list[GitCommitSummary] = []
    for repo in repo_paths:
        if not (repo / ".git").exists():
            continue
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    f"--since={since}",
                    f"--max-count={max_per_repo}",
                    "--pretty=format:%H%x09%at%x09%ae%x09%s",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, ts, email, subject = parts
            try:
                ts_f = float(ts)
            except ValueError:
                continue
            out.append(
                GitCommitSummary(
                    repo_path=str(repo.resolve()),
                    sha=sha[:12],
                    timestamp=ts_f,
                    subject=subject[:200],
                    author_email=email[:128],
                )
            )
    return out
