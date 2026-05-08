"""RewindStore — on-disk content-hashed checkpoint storage with hygiene.

Layout::

    root/
      <checkpoint_id>/
        meta.json
        files/<path-slash-escaped>
      [subagents/<subagent_id>/<checkpoint_id>/...]
      .last_prune                      ← auto-prune marker (mtime tracked)
      .pending_delete/<id>/...         ← atomic-delete staging (transient)

``restore()`` writes files back to ``workspace_root``. ``save_shielded()``
wraps the write in :func:`asyncio.shield` so a Ctrl-C mid-save can't
corrupt the store.

Hygiene additions (2026-05-08):

- ``total_size_bytes`` / ``count`` / ``oldest`` / ``newest`` accessors.
- ``prune`` with age + count + size policies + atomic ``.pending_delete``
  staging that survives a crash mid-prune.
- ``clear`` to wipe (preserving the auto-prune marker).
- ``should_auto_prune`` / ``mark_pruned`` for cooperative auto-prune
  scheduling (used by the ``auto_checkpoint`` PreToolUse hook).
- ``save(cp, *, max_total_bytes=...)`` evicts oldest checkpoints
  before write to keep aggregate size under cap.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .checkpoint import Checkpoint


@dataclass(frozen=True)
class PruneReport:
    """Outcome of :meth:`RewindStore.prune`."""

    dropped: tuple[str, ...]
    kept: int
    orphans_removed: tuple[str, ...]
    bytes_freed: int
    bytes_remaining: int
    dry_run: bool


class RewindStore:
    PENDING_DELETE_DIR = ".pending_delete"
    LAST_PRUNE_MARKER = ".last_prune"

    def __init__(
        self,
        root: Path,
        workspace_root: Path | None = None,
        *,
        subagent_id: str | None = None,
    ):
        base = Path(root)
        self.root = base / "subagents" / subagent_id if subagent_id else base
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self.subagent_id = subagent_id
        # Per-instance flag preventing concurrent auto-prune fires within
        # the same process. See should_auto_prune / mark_prune_started /
        # mark_prune_finished.
        self._prune_in_flight = False

    # ─── save / load ────────────────────────────────────────────

    def save(self, cp: Checkpoint, *, max_total_bytes: int | None = None) -> None:
        """Persist ``cp``. If ``max_total_bytes`` is set, evict oldest first.

        Eviction is in-line (not via :meth:`prune`) so it's guaranteed
        to run synchronously before the new checkpoint lands.

        Eviction reads metadata only (not file blobs) so the cost is
        O(N file-stat calls) per save, not O(N × blob_bytes loaded into RAM).
        """
        if max_total_bytes is not None:
            cp_size_estimate = sum(len(b) for b in cp.files.values()) + 1024
            running_total = self.total_size_bytes(include_subagents=False)
            metas = sorted(self._iter_metadata(), key=lambda t: t[1])
            i = 0
            while (
                running_total + cp_size_estimate > max_total_bytes
                and i < len(metas)
            ):
                _cid, _created, oldest_path, oldest_size = metas[i]
                shutil.rmtree(oldest_path, ignore_errors=True)
                running_total -= oldest_size
                i += 1

        cp_dir = self.root / cp.id
        cp_dir.mkdir(exist_ok=True)
        (cp_dir / "meta.json").write_text(
            json.dumps(
                {
                    "id": cp.id,
                    "label": cp.label,
                    "created_at": cp.created_at,
                    "paths": list(cp.files.keys()),
                    "excluded_files": list(cp.excluded_files),
                }
            )
        )
        files_dir = cp_dir / "files"
        files_dir.mkdir(exist_ok=True)
        for path, data in cp.files.items():
            safe = path.replace("/", "__")
            (files_dir / safe).write_bytes(data)

    async def save_shielded(self, cp: Checkpoint) -> None:
        """Shielded from cancellation so Ctrl-C mid-save can't corrupt."""
        await asyncio.shield(asyncio.to_thread(self.save, cp))

    def load(self, checkpoint_id: str) -> Checkpoint | None:
        cp_dir = self.root / checkpoint_id
        meta_path = cp_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except (ValueError, OSError):
            return None
        files: dict[str, bytes] = {}
        for path in meta.get("paths", []):
            safe = path.replace("/", "__")
            try:
                files[path] = (cp_dir / "files" / safe).read_bytes()
            except OSError:
                return None
        return Checkpoint(
            id=meta["id"],
            files=files,
            label=meta.get("label", ""),
            created_at=meta.get("created_at", ""),
            excluded_files=tuple(meta.get("excluded_files", [])),
        )

    # ─── enumeration + restore ──────────────────────────────────

    def list(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        if not self.root.exists():
            return out
        for cp_dir in self.root.iterdir():
            if not cp_dir.is_dir():
                continue
            if cp_dir.name in (
                "subagents",
                self.PENDING_DELETE_DIR,
            ) or cp_dir.name.startswith("."):
                continue
            cp = self.load(cp_dir.name)
            if cp is not None:
                out.append(cp)
        return sorted(out, key=lambda c: c.created_at, reverse=True)

    def restore(self, checkpoint_id: str) -> None:
        cp = self.load(checkpoint_id)
        if cp is None:
            raise KeyError(checkpoint_id)
        for rel_path, data in cp.files.items():
            target = self.workspace_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    # ─── size + count + age ────────────────────────────────────

    def total_size_bytes(self, *, include_subagents: bool = True) -> int:
        """Recursive disk usage of ``self.root`` in bytes.

        When ``include_subagents=False``, excludes the ``subagents/``
        subtree. Best-effort: silently swallows :class:`OSError` on
        individual files.
        """
        if not self.root.exists():
            return 0
        total = 0
        for entry in self.root.rglob("*"):
            if not include_subagents and "subagents" in entry.parts:
                continue
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
        return total

    def count(self, *, include_subagents: bool = True) -> int:
        """Number of checkpoint dirs under ``self.root``.

        A checkpoint dir = a child of ``self.root`` (or of any
        ``subagents/<id>/`` subdir) that contains a ``meta.json``.
        Hidden dirs (``.last_prune``, ``.pending_delete``) are
        excluded.
        """
        if not self.root.exists():
            return 0
        total = 0
        for child in self.root.iterdir():
            if child.name == "subagents":
                if include_subagents:
                    for sa in child.iterdir():
                        if sa.is_dir():
                            for cp_dir in sa.iterdir():
                                if cp_dir.is_dir() and (cp_dir / "meta.json").exists():
                                    total += 1
                continue
            if child.name.startswith("."):
                continue
            if child.is_dir() and (child / "meta.json").exists():
                total += 1
        return total

    def _iter_metadata(self):
        """Yield ``(id, created_at_iso, dir_path, size_bytes)`` per checkpoint.

        Reads ``meta.json`` only — does NOT load file blobs into RAM.
        Used by :meth:`save` eviction and :meth:`oldest` / :meth:`newest`
        to avoid O(N × blob_bytes) memory thrash on large stores.
        """
        if not self.root.exists():
            return
        for child in self.root.iterdir():
            if child.name in (self.PENDING_DELETE_DIR, "subagents"):
                continue
            if child.name.startswith("."):
                continue
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (ValueError, OSError):
                continue
            cid = str(meta.get("id", ""))
            created = str(meta.get("created_at", ""))
            try:
                size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
            except OSError:
                size = 0
            yield (cid, created, child, size)

    def oldest(self) -> Checkpoint | None:
        """Oldest valid checkpoint by ``created_at``.

        Loads ONLY the chosen checkpoint's blobs (not all blobs).
        """
        metas = list(self._iter_metadata())
        if not metas:
            return None
        metas.sort(key=lambda t: t[1])  # ascending → oldest first
        return self.load(metas[0][0])

    def newest(self) -> Checkpoint | None:
        """Newest valid checkpoint by ``created_at``.

        Loads ONLY the chosen checkpoint's blobs (not all blobs).
        """
        metas = list(self._iter_metadata())
        if not metas:
            return None
        metas.sort(key=lambda t: t[1], reverse=True)
        return self.load(metas[0][0])

    # ─── prune + clear ─────────────────────────────────────────

    def prune(
        self,
        *,
        older_than_days: int | None = None,
        max_total_bytes: int | None = None,
        max_count: int | None = None,
        delete_orphans: bool = True,
        dry_run: bool = False,
    ) -> PruneReport:
        """Apply prune policy. Returns a :class:`PruneReport`.

        Order: orphans → age → count → size. Eviction within each
        criterion is oldest-first (by ``created_at``).

        Atomicity: each scheduled dir is :func:`os.replace` d into
        ``<root>/.pending_delete/<id>`` before the final
        :func:`shutil.rmtree`, so a crash mid-prune leaves recoverable
        state — the next prune will sweep the leftover.
        """
        if not self.root.exists():
            return PruneReport(
                dropped=(),
                kept=0,
                orphans_removed=(),
                bytes_freed=0,
                bytes_remaining=0,
                dry_run=dry_run,
            )

        # Recover any prior pending-delete directories from a crashed run.
        pending = self.root / self.PENDING_DELETE_DIR
        if pending.exists() and not dry_run:
            for child in list(pending.iterdir()):
                shutil.rmtree(child, ignore_errors=True)
            try:
                pending.rmdir()
            except OSError:
                pass

        valid: list[tuple[str, str, Path, int]] = []
        orphans: list[Path] = []

        for child in self.root.iterdir():
            if child.name == self.PENDING_DELETE_DIR:
                continue
            if child.name == "subagents":
                continue
            if child.name.startswith("."):
                continue
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                orphans.append(child)
                continue
            try:
                meta = json.loads(meta_path.read_text())
                cid = str(meta["id"])
                created = str(meta.get("created_at", ""))
            except (ValueError, OSError, KeyError):
                orphans.append(child)
                continue
            size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
            valid.append((cid, created, child, size))

        # Sort newest-first by created_at; oldest-first eviction reverses.
        valid.sort(key=lambda t: t[1], reverse=True)

        scheduled_for_drop: list[tuple[str, Path, int]] = []

        # 1. Age
        if older_than_days is not None:
            threshold = datetime.now(UTC) - timedelta(days=older_than_days)
            keep: list[tuple[str, str, Path, int]] = []
            for cid, created, path, size in valid:
                try:
                    when = datetime.fromisoformat(created)
                except ValueError:
                    keep.append((cid, created, path, size))
                    continue
                if when < threshold:
                    scheduled_for_drop.append((cid, path, size))
                else:
                    keep.append((cid, created, path, size))
            valid = keep

        # 2. Count cap (drop oldest above cap)
        if max_count is not None and len(valid) > max_count:
            survivors = valid[:max_count]
            evict = valid[max_count:]
            for cid, _c, path, size in evict:
                scheduled_for_drop.append((cid, path, size))
            valid = survivors

        # 3. Size cap (drop oldest until under cap)
        if max_total_bytes is not None:
            survivors = list(valid)
            total_now = sum(s for _i, _c, _p, s in survivors)
            while total_now > max_total_bytes and survivors:
                cid, _c, path, size = survivors.pop()  # oldest = last
                scheduled_for_drop.append((cid, path, size))
                total_now -= size
            valid = survivors

        bytes_freed = sum(s for _, _, s in scheduled_for_drop)
        if delete_orphans:
            bytes_freed += sum(
                p.stat().st_size for orph in orphans for p in orph.rglob("*") if p.is_file()
            )

        if dry_run:
            return PruneReport(
                dropped=tuple(cid for cid, _, _ in scheduled_for_drop),
                kept=len(valid),
                orphans_removed=tuple(o.name for o in orphans) if delete_orphans else (),
                bytes_freed=bytes_freed,
                bytes_remaining=max(0, sum(s for _i, _c, _p, s in valid)),
                dry_run=True,
            )

        pending.mkdir(parents=True, exist_ok=True)
        targets: list[Path] = [p for _, p, _ in scheduled_for_drop]
        if delete_orphans:
            targets.extend(orphans)
        for t in targets:
            try:
                staged = pending / t.name
                if staged.exists():
                    shutil.rmtree(staged, ignore_errors=True)
                t.replace(staged)
                shutil.rmtree(staged, ignore_errors=True)
            except OSError:
                pass
        try:
            pending.rmdir()
        except OSError:
            pass

        return PruneReport(
            dropped=tuple(cid for cid, _, _ in scheduled_for_drop),
            kept=len(valid),
            orphans_removed=tuple(o.name for o in orphans) if delete_orphans else (),
            bytes_freed=bytes_freed,
            bytes_remaining=sum(s for _, _, _, s in valid),
            dry_run=False,
        )

    def clear(self) -> int:
        """Wipe all checkpoint dirs (preserve ``.last_prune``).

        Returns the count of checkpoints cleared.
        """
        n = self.count(include_subagents=True)
        if not self.root.exists():
            return 0
        for child in list(self.root.iterdir()):
            if child.name == self.LAST_PRUNE_MARKER:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
        return n

    # ─── auto-prune cooperation ─────────────────────────────────

    def should_auto_prune(self, *, min_interval_hours: int = 24) -> bool:
        """True iff a prune is due AND not already in flight on this instance.

        Checks two things:

        1. The in-process ``_prune_in_flight`` flag — prevents two
           concurrent fires within the same process from both scheduling
           a prune.
        2. The on-disk ``.last_prune`` marker — if present and younger
           than ``min_interval_hours``, prune is not due yet.

        Returns False if EITHER (1) or (2) say "no" — i.e. only returns
        True when this instance currently holds no prune AND the
        on-disk record says enough time has elapsed.
        """
        if self._prune_in_flight:
            return False
        marker = self.root / self.LAST_PRUNE_MARKER
        if not marker.exists():
            return True
        try:
            age_h = (time.time() - marker.stat().st_mtime) / 3600.0
        except OSError:
            return True
        return age_h >= min_interval_hours

    def mark_prune_started(self) -> None:
        """Claim the auto-prune slot for THIS instance.

        Sets ``_prune_in_flight = True`` so concurrent ``should_auto_prune``
        calls return False until :meth:`mark_prune_finished` runs.
        """
        self._prune_in_flight = True

    def mark_prune_finished(self, *, success: bool) -> None:
        """Release the in-flight slot. Persist ``.last_prune`` ONLY on success.

        The on-disk marker is the cooperative cross-call signal. Writing
        it on failure would block the next 24h of retries despite the
        prune never having actually run. Writing only on success means
        a transient failure (e.g. permission denied) is naturally
        retried on the next handler invocation.
        """
        self._prune_in_flight = False
        if success:
            self.mark_pruned()

    def mark_pruned(self) -> None:
        """Touch ``.last_prune`` atomically.

        Public so manual ``oc checkpoints prune`` invocations can record
        their own completion. The auto-prune path goes through
        :meth:`mark_prune_finished` instead.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / self.LAST_PRUNE_MARKER
        tmp = self.root / f".last_prune.tmp.{secrets.token_hex(4)}"
        tmp.write_text(datetime.now(UTC).isoformat())
        os.replace(tmp, marker)


__all__ = ["PruneReport", "RewindStore"]
