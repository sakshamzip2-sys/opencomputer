"""Atomic text / bytes / JSON writes with mandatory fsync.

Sequence: open sibling tmp in target directory -> write -> fsync(fd) ->
os.replace(tmp, target). The rename is atomic on POSIX and Windows>=Vista
within a single filesystem; the fsync makes the data durable across power
loss as well, which OpenClaw's output-atomic.ts skipped (BLUEPRINT bug fix).

On any exception the tmp file is best-effort removed.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

_TMP_PREFIX = ".browser-port-tmp-"


def _sibling_tmp(target: Path) -> Path:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    return parent / f"{_TMP_PREFIX}{token}-{target.name}.part"


def atomic_write_bytes(path: str | os.PathLike[str], content: bytes) -> None:
    target = Path(path)
    tmp = _sibling_tmp(target)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("os.write returned non-positive count")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    atomic_write_bytes(path, content.encode(encoding))


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int | None = 2,
) -> None:
    serialized = json.dumps(data, indent=indent, ensure_ascii=False)
    if not serialized.endswith("\n"):
        serialized += "\n"
    atomic_write_text(path, serialized)
