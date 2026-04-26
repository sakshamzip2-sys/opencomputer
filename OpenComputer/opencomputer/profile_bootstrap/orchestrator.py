"""Bootstrap orchestrator — sequences Layers 0/1/2 for a single install run.

Called by the ``opencomputer profile bootstrap`` CLI subcommand. Each
layer is independent and best-effort — a failure in one does not
block subsequent layers.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.profile_bootstrap.identity_reflex import gather_identity
from opencomputer.profile_bootstrap.persistence import (
    write_identity_to_graph,
    write_interview_answers_to_graph,
)
from opencomputer.profile_bootstrap.recent_scan import (
    scan_git_log,
    scan_recent_files,
)
from opencomputer.user_model.store import UserModelStore

_log = logging.getLogger("opencomputer.profile_bootstrap.orchestrator")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Summary of one bootstrap pass for CLI display + audit log."""

    identity_nodes_written: int = 0
    interview_nodes_written: int = 0
    files_scanned: int = 0
    git_commits_scanned: int = 0
    elapsed_seconds: float = 0.0


def run_bootstrap(
    *,
    interview_answers: dict[str, str],
    scan_roots: list[Path],
    git_repos: list[Path],
    include_calendar: bool = True,
    include_browser_history: bool = True,
    store: UserModelStore | None = None,
    marker_path: Path | None = None,
) -> BootstrapResult:
    """Run all MVP bootstrap layers and persist outputs to the user-model graph.

    Marker write at the end is the "bootstrap completed" signal the CLI
    checks on subsequent runs.
    """
    started = time.monotonic()
    s = store if store is not None else UserModelStore()

    # Layer 0
    facts = gather_identity()
    identity_n = write_identity_to_graph(facts, store=s)

    # Layer 1
    interview_n = write_interview_answers_to_graph(interview_answers, store=s)

    # Layer 2 — files
    files = scan_recent_files(roots=scan_roots, days=7) if scan_roots else []

    # Layer 2 — git
    commits = scan_git_log(repo_paths=git_repos, days=7) if git_repos else []

    # Layer 2 — calendar / browser are passed through here in MVP only as
    # counters; the LLM-extraction-and-importer wiring lands in V2 to
    # avoid blocking MVP on Ollama install. For MVP we simply log + count.
    if include_calendar:
        try:
            from opencomputer.profile_bootstrap.calendar_reader import (
                read_upcoming_events,
            )
            _ = read_upcoming_events(days=7)
        except Exception:  # noqa: BLE001
            _log.exception("calendar read failed")

    if include_browser_history:
        try:
            from opencomputer.profile_bootstrap.browser_history import (
                read_chrome_history,
            )
            _ = read_chrome_history(days=7)
        except Exception:  # noqa: BLE001
            _log.exception("browser history read failed")

    elapsed = time.monotonic() - started
    result = BootstrapResult(
        identity_nodes_written=identity_n,
        interview_nodes_written=interview_n,
        files_scanned=len(files),
        git_commits_scanned=len(commits),
        elapsed_seconds=elapsed,
    )

    if marker_path is not None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({**asdict(result), "completed_at": time.time()}))

    return result
