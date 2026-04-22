"""RewindStore — on-disk content-hashed checkpoint storage.

Layout::

    root/
      <checkpoint_id>/
        meta.json
        files/<path-slash-escaped>

`restore()` writes files back to `workspace_root`. `save_shielded()` wraps the
write in `asyncio.shield()` so a Ctrl-C mid-save can't corrupt the store.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .checkpoint import Checkpoint


class RewindStore:
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

    # ─── save / load ────────────────────────────────────────────

    def save(self, cp: Checkpoint) -> None:
        cp_dir = self.root / cp.id
        cp_dir.mkdir(exist_ok=True)
        (cp_dir / "meta.json").write_text(
            json.dumps(
                {
                    "id": cp.id,
                    "label": cp.label,
                    "created_at": cp.created_at,
                    "paths": list(cp.files.keys()),
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
        meta = json.loads(meta_path.read_text())
        files: dict[str, bytes] = {}
        for path in meta["paths"]:
            safe = path.replace("/", "__")
            files[path] = (cp_dir / "files" / safe).read_bytes()
        return Checkpoint(
            id=meta["id"],
            files=files,
            label=meta["label"],
            created_at=meta["created_at"],
        )

    # ─── enumeration + restore ──────────────────────────────────

    def list(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        if not self.root.exists():
            return out
        for cp_dir in self.root.iterdir():
            if cp_dir.is_dir() and cp_dir.name != "subagents":
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


__all__ = ["RewindStore"]
