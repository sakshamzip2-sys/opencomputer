"""Cross-session checkpoint admin — backs ``oc checkpoints status/prune/clear``.

Walks ``<harness_root>/*/rewind/`` (one rewind store per session) and
provides aggregate views + bulk operations. Each :class:`StoreInfo`
rolls subagent dirs into the parent session's totals.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Coding-harness lives outside the opencomputer package; add to path lazily.
_HARNESS = Path(__file__).resolve().parents[1] / "extensions" / "coding-harness"
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))

from rewind.store import PruneReport, RewindStore  # type: ignore[import-not-found]  # noqa: E402

logger = logging.getLogger("opencomputer.cli.checkpoints")


@dataclass(frozen=True, slots=True)
class PrunePolicy:
    """Bundle of prune flags. :meth:`from_config` produces sensible defaults."""

    older_than_days: int | None = None
    max_total_bytes: int | None = None
    max_count: int | None = None
    delete_orphans: bool = True
    dry_run: bool = False

    @classmethod
    def from_config(cls, cfg) -> "PrunePolicy":  # type: ignore[no-untyped-def]
        """Build from CheckpointsConfig (live config or test stub)."""
        return cls(
            older_than_days=cfg.retention_days,
            max_total_bytes=cfg.max_total_size_mb * 1024 * 1024,
            max_count=cfg.max_snapshots,
            delete_orphans=cfg.delete_orphans,
            dry_run=False,
        )


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """One session's checkpoint store summary."""

    session_id: str
    path: Path
    count: int
    size_bytes: int
    oldest_iso: str | None
    newest_iso: str | None
    last_prune_iso: str | None
    subagent_count: int


@dataclass(frozen=True, slots=True)
class AggregateReport:
    stores: tuple[StoreInfo, ...]
    total_size_bytes: int
    total_count: int


def harness_root() -> Path:
    """Return ``<OPENCOMPUTER_HOME_ROOT or ~/.opencomputer>/harness/``."""
    override = os.environ.get("OPENCOMPUTER_HOME_ROOT")
    base = Path(override) if override else Path.home() / ".opencomputer"
    return base / "harness"


def iter_stores() -> Iterator[StoreInfo]:
    """Yield one :class:`StoreInfo` per session under :func:`harness_root`.

    Subagent dirs are FLATTENED into the parent session's count/size.
    Sessions whose ``rewind/`` dir is empty are still yielded with
    count=0 — callers can filter them if they prefer hiding empties.
    """
    root = harness_root()
    if not root.exists():
        return
    for sess in sorted(root.iterdir()):
        if not sess.is_dir():
            continue
        rwd = sess / "rewind"
        if not rwd.exists():
            continue
        try:
            store = RewindStore(rwd, workspace_root=sess)
            cnt = store.count(include_subagents=True)
            size = store.total_size_bytes(include_subagents=True)
            oldest = store.oldest()
            newest = store.newest()
            marker = rwd / RewindStore.LAST_PRUNE_MARKER
            last_prune = (
                datetime.fromtimestamp(marker.stat().st_mtime).isoformat()
                if marker.exists()
                else None
            )
            sub_count = 0
            sub_dir = rwd / "subagents"
            if sub_dir.exists():
                sub_count = sum(1 for child in sub_dir.iterdir() if child.is_dir())
            yield StoreInfo(
                session_id=sess.name,
                path=rwd,
                count=cnt,
                size_bytes=size,
                oldest_iso=oldest.created_at if oldest else None,
                newest_iso=newest.created_at if newest else None,
                last_prune_iso=last_prune,
                subagent_count=sub_count,
            )
        except (OSError, ValueError) as exc:
            logger.warning("could not read store %s: %s", rwd, exc)
            continue


def aggregate_status() -> AggregateReport:
    stores = tuple(iter_stores())
    return AggregateReport(
        stores=stores,
        total_size_bytes=sum(s.size_bytes for s in stores),
        total_count=sum(s.count for s in stores),
    )


def prune_all(
    *,
    policy: PrunePolicy,
    session_filter: str | None = None,
) -> dict[str, PruneReport]:
    """Apply ``policy`` to every (or one) store. Returns ``{session_id: report}``."""
    out: dict[str, PruneReport] = {}
    for info in iter_stores():
        if session_filter and info.session_id != session_filter:
            continue
        store = RewindStore(info.path, workspace_root=info.path.parent)
        report = store.prune(
            older_than_days=policy.older_than_days,
            max_total_bytes=policy.max_total_bytes,
            max_count=policy.max_count,
            delete_orphans=policy.delete_orphans,
            dry_run=policy.dry_run,
        )
        if not policy.dry_run:
            store.mark_pruned()
        out[info.session_id] = report
    return out


def clear_all(*, session_filter: str | None = None) -> int:
    """Wipe checkpoints across all (or one) session stores. Returns total cleared."""
    total = 0
    for info in iter_stores():
        if session_filter and info.session_id != session_filter:
            continue
        store = RewindStore(info.path, workspace_root=info.path.parent)
        total += store.clear()
    return total


__all__ = [
    "AggregateReport",
    "PrunePolicy",
    "StoreInfo",
    "aggregate_status",
    "clear_all",
    "harness_root",
    "iter_stores",
    "prune_all",
]
