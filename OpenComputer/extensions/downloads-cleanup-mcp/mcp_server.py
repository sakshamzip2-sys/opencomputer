#!/usr/bin/env python3
"""Downloads-cleanup MCP server (mcp-openclaw-port M5 reference).

A standalone MCP server bundled with the ``downloads-cleanup-mcp``
plugin. Demonstrates the production-grade authoring surface:

* stdio transport (most common for bundle MCPs).
* Three real tools that touch the user filesystem.
* Strict input validation — bounded path operations, scope guard so
  this server can only ever read / move things under ``~/Downloads``.
* Logging to stderr so operators can debug spawn / lifecycle.

Tools:

* ``list_downloads(min_age_days: int = 0, limit: int = 200)`` —
  enumerate files under ``~/Downloads`` older than ``min_age_days``,
  returning name + extension + size + mtime.
* ``summarise_downloads(by: str = "extension")`` — group counts +
  cumulative size by extension or by age bucket
  (``today | 7d | 30d | older``).
* ``archive_old(min_age_days: int = 30, dest_subdir: str = "_archive")``
  — move stale files into ``~/Downloads/<dest_subdir>/`` (created
  if absent). Returns the manifest of moved files.

Safety:

* ``~/Downloads`` is the ONLY directory this server is allowed to
  read or write. Any path that would resolve outside it raises.
* The archive destination is rooted under ``~/Downloads`` by
  construction, so the cleanup path is non-destructive — files
  move, never delete.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# Lazy import: ``mcp`` is the official MCP SDK, brought in by OC's
# pyproject. A bundle MCP shipped by a third-party plugin would pin
# this directly in the plugin's own deps. We rely on OC's existing
# install to surface the SDK; the bundle ships nothing extra here.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover — defensive only
    print(
        "downloads-cleanup-mcp: missing 'mcp' SDK — install via `pip install mcp`",
        file=sys.stderr,
    )
    sys.exit(1)


logger = logging.getLogger("downloads_cleanup_mcp")
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [downloads-cleanup-mcp] %(levelname)s %(message)s",
)


def _downloads_root() -> Path:
    """Return the absolute, resolved ``~/Downloads`` path.

    Lifted to a function so test harnesses can monkeypatch the home
    via ``$HOME`` override without restarting the server.
    """
    return (Path.home() / "Downloads").resolve()


def _assert_inside_downloads(p: Path) -> Path:
    """Resolve + assert the path sits inside ``~/Downloads``. Returns the resolved path.

    Raises ``ValueError`` on escape. This is the single safety perimeter
    for every write op — no shell, no os.system, no chdir.
    """
    resolved = p.resolve()
    root = _downloads_root()
    try:
        _ = resolved.relative_to(root)
    except ValueError as e:
        raise ValueError(
            f"path {p!s} escapes ~/Downloads ({root!s})"
        ) from e
    return resolved


def build_server() -> FastMCP:
    """Construct the MCP server with all three tools registered."""
    server = FastMCP(
        name="downloads-cleanup",
        instructions=(
            "Cleanup tools for the user's ~/Downloads folder. Read-only "
            "listings + categorise + move-to-archive. Never deletes files."
        ),
    )

    @server.tool()
    def list_downloads(
        min_age_days: int = 0, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List files under ``~/Downloads`` older than ``min_age_days``.

        Args:
            min_age_days: Skip files whose mtime is newer than this.
                Default 0 = list everything.
            limit: Cap on result count (defensive — UI-only listing).

        Returns:
            List of dicts: ``path``, ``name``, ``extension``, ``size``,
            ``mtime`` (unix seconds), ``age_days``.
        """
        bounded_age = max(0, int(min_age_days))
        bounded_limit = max(1, min(int(limit), 2000))
        root = _downloads_root()
        if not root.exists():
            logger.warning("~/Downloads does not exist (%s) — returning empty", root)
            return []
        now = time.time()
        cutoff = now - bounded_age * 86400.0
        out: list[dict[str, Any]] = []
        for entry in root.iterdir():
            try:
                stat = entry.stat()
            except OSError:
                continue
            if not entry.is_file():
                continue
            if stat.st_mtime > cutoff:
                continue
            out.append({
                "path": str(entry),
                "name": entry.name,
                "extension": entry.suffix.lstrip(".") or "",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "age_days": (now - stat.st_mtime) / 86400.0,
            })
            if len(out) >= bounded_limit:
                break
        # Sort by mtime ascending (oldest first — the candidate for cleanup).
        out.sort(key=lambda r: r["mtime"])
        return out

    @server.tool()
    def summarise_downloads(by: str = "extension") -> dict[str, Any]:
        """Summarise ``~/Downloads`` contents — group counts + total size.

        Args:
            by: ``"extension"`` (default) groups by file extension;
                ``"age"`` groups into ``today | 7d | 30d | older`` buckets.

        Returns:
            Dict with ``total_files``, ``total_size_bytes``, and
            ``groups`` mapping the bucket key to ``{count, size_bytes}``.
        """
        mode = (by or "extension").strip().lower()
        if mode not in ("extension", "age"):
            return {"error": f"by must be 'extension' or 'age', got {by!r}"}
        root = _downloads_root()
        groups: dict[str, dict[str, int]] = defaultdict(
            lambda: {"count": 0, "size_bytes": 0},
        )
        total_files = 0
        total_size = 0
        if not root.exists():
            return {
                "total_files": 0,
                "total_size_bytes": 0,
                "groups": {},
            }
        now = time.time()
        for entry in root.iterdir():
            try:
                stat = entry.stat()
            except OSError:
                continue
            if not entry.is_file():
                continue
            total_files += 1
            total_size += stat.st_size
            if mode == "extension":
                key = entry.suffix.lstrip(".").lower() or "<noext>"
            else:
                age_days = (now - stat.st_mtime) / 86400.0
                if age_days < 1:
                    key = "today"
                elif age_days < 7:
                    key = "7d"
                elif age_days < 30:
                    key = "30d"
                else:
                    key = "older"
            groups[key]["count"] += 1
            groups[key]["size_bytes"] += stat.st_size
        return {
            "total_files": total_files,
            "total_size_bytes": total_size,
            "groups": dict(groups),
        }

    @server.tool()
    def archive_old(
        min_age_days: int = 30, dest_subdir: str = "_archive",
    ) -> dict[str, Any]:
        """Move files older than ``min_age_days`` into ``~/Downloads/<dest_subdir>/``.

        Non-destructive — files are MOVED, never deleted. The destination
        is rooted under ``~/Downloads`` so the operation cannot escape
        the cleanup directory.

        Args:
            min_age_days: Only move files older than this many days.
                Must be at least 1 (refuses 0 — that would mean moving
                everything, which is almost certainly an operator error).
            dest_subdir: Subdirectory name under ``~/Downloads`` to use
                as the archive bucket. Created if absent. Must be a
                simple name (no path separators).

        Returns:
            Dict with ``moved`` (list of ``{src, dest}`` pairs) and
            ``skipped`` (list of files that errored, with a reason).
        """
        if int(min_age_days) < 1:
            return {
                "error": "min_age_days must be >= 1 (refusing to archive everything)"
            }
        if "/" in dest_subdir or "\\" in dest_subdir or dest_subdir.startswith("."):
            return {
                "error": f"dest_subdir {dest_subdir!r} must be a simple name "
                "(no path separators, no leading '.')"
            }
        root = _downloads_root()
        if not root.exists():
            return {"moved": [], "skipped": [], "note": "~/Downloads not present"}
        dest = root / dest_subdir
        dest.mkdir(exist_ok=True)
        try:
            _assert_inside_downloads(dest)
        except ValueError as e:
            return {"error": f"dest sanity check failed: {e}"}
        cutoff = time.time() - int(min_age_days) * 86400.0
        moved: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for entry in root.iterdir():
            try:
                stat = entry.stat()
            except OSError as e:
                skipped.append({"path": str(entry), "reason": f"stat failed: {e}"})
                continue
            if not entry.is_file():
                continue
            if stat.st_mtime > cutoff:
                continue
            target = dest / entry.name
            if target.exists():
                # De-collide by appending a numeric suffix.
                stem = entry.stem
                ext = entry.suffix
                for n in range(1, 100):
                    candidate = dest / f"{stem}-{n}{ext}"
                    if not candidate.exists():
                        target = candidate
                        break
                else:
                    skipped.append({
                        "path": str(entry),
                        "reason": "100 collisions — refusing",
                    })
                    continue
            try:
                _ = _assert_inside_downloads(target)
                entry.rename(target)
                moved.append({"src": str(entry), "dest": str(target)})
            except (OSError, ValueError) as e:
                skipped.append({"path": str(entry), "reason": f"move failed: {e}"})
        return {"moved": moved, "skipped": skipped}

    return server


async def _run() -> None:
    server = build_server()
    logger.info("downloads-cleanup-mcp server starting on stdio")
    await server.run_stdio_async()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("downloads-cleanup-mcp stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
