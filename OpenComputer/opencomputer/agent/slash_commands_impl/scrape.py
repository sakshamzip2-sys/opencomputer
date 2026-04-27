"""``/scrape`` — invoke the profile-scraper skill (V3.A-T10).

Surfaces the V3.A-T2 :func:`opencomputer.skills.profile_scraper.scraper.run_scrape`
entry point so the user can trigger a scrape inline without leaving chat.

Usage::

    /scrape              → run a scrape, write a snapshot, return a 3-line summary
    /scrape --full       → same (the ``full`` flag is reserved by run_scrape, V3.B)
    /scrape --diff       → diff the two most recent snapshots; never re-runs scrape

The ``--diff`` form is read-only — it never re-runs the scraper, just
inspects the snapshot files in ``<profile_home>/profile_scraper/`` and
returns added / removed / changed facts as a short bulleted summary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _parse_args(args: str) -> tuple[bool, bool]:
    """Return ``(full, diff)`` from the raw argstring.

    Tokenisation is whitespace-split — flags can appear in any order or
    repeat without harm. Unknown tokens are silently ignored so future
    flags don't break older callers.
    """
    tokens = args.split()
    full = "--full" in tokens
    diff = "--diff" in tokens
    return full, diff


def _list_snapshot_files() -> list[Path]:
    """Return snapshot JSON files sorted oldest→newest, ``[]`` if none."""
    out_dir = _home() / "profile_scraper"
    if not out_dir.exists():
        return []
    # Exclude latest.json (the pointer copy) — only walk the dated files.
    return sorted(out_dir.glob("snapshot_*.json"))


def _load_facts(path: Path) -> set[tuple[str, str]]:
    """Load a snapshot's facts as a hashable ``(field, value)`` set.

    Values that aren't ``str`` (e.g. dict-shaped browser visits) are
    JSON-serialised with sorted keys so the same logical fact compares
    equal across snapshots regardless of dict ordering.
    """
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    out: set[tuple[str, str]] = set()
    for fact in payload.get("facts", []) or []:
        field = fact.get("field", "")
        value = fact.get("value", "")
        if not isinstance(value, str):
            try:
                value = json.dumps(value, sort_keys=True)
            except (TypeError, ValueError):
                value = str(value)
        out.add((field, value))
    return out


def _diff_snapshots() -> str:
    """Compute a human-readable diff between the two most recent snapshots.

    Returns the diff body string. When fewer than two snapshots exist on
    disk the caller is told so — the agent should run ``/scrape`` first.
    """
    files = _list_snapshot_files()
    if not files:
        return (
            "No prior snapshot to diff. Run `/scrape` first to "
            "establish a baseline."
        )
    if len(files) < 2:
        return (
            "Only one snapshot on disk — no diff possible yet. "
            f"Run `/scrape` again to capture changes against {files[-1].name}."
        )
    older, newer = files[-2], files[-1]
    old_facts = _load_facts(older)
    new_facts = _load_facts(newer)

    added = sorted(new_facts - old_facts)
    removed = sorted(old_facts - new_facts)

    # Detect "changed" entries — same field, different value. We can only
    # do this when the field appears exactly once on each side, otherwise
    # we'd guess a pairing arbitrarily.
    added_by_field: dict[str, list[str]] = {}
    removed_by_field: dict[str, list[str]] = {}
    for f, v in added:
        added_by_field.setdefault(f, []).append(v)
    for f, v in removed:
        removed_by_field.setdefault(f, []).append(v)
    changed: list[tuple[str, str, str]] = []
    for field in list(added_by_field.keys()):
        if (
            field in removed_by_field
            and len(added_by_field[field]) == 1
            and len(removed_by_field[field]) == 1
        ):
            changed.append(
                (field, removed_by_field[field][0], added_by_field[field][0])
            )
            added_by_field.pop(field)
            removed_by_field.pop(field)

    lines: list[str] = [
        f"Diff: {older.name} → {newer.name}",
    ]
    if not added_by_field and not removed_by_field and not changed:
        lines.append("(no changes)")
        return "\n".join(lines)
    if changed:
        lines.append("Changed:")
        for field, old, new in changed:
            lines.append(f"  ~ {field}: {old} -> {new}")
    if added_by_field:
        lines.append("Added:")
        for field, values in sorted(added_by_field.items()):
            for v in values:
                lines.append(f"  + {field}: {v}")
    if removed_by_field:
        lines.append("Removed:")
        for field, values in sorted(removed_by_field.items()):
            for v in values:
                lines.append(f"  - {field}: {v}")
    return "\n".join(lines)


class ScrapeCommand(SlashCommand):
    """``/scrape [--full] [--diff]`` — invoke the profile-scraper skill."""

    name: str = "scrape"
    description: str = (
        "Run the profile-scraper skill (writes a snapshot under "
        "<profile_home>/profile_scraper/). Flags: --full forces a full "
        "scrape (reserved); --diff compares the two most recent snapshots."
    )

    async def execute(
        self, args: str, runtime: Any
    ) -> SlashCommandResult:
        full, diff = _parse_args(args)

        if diff:
            try:
                output = _diff_snapshots()
            except Exception as exc:  # noqa: BLE001 — defensive; never propagate
                output = (
                    f"`/scrape --diff` failed: {type(exc).__name__}: {exc}"
                )
            return SlashCommandResult(output=output, handled=True)

        # Default behaviour: run the scraper. ``full`` is reserved per
        # T2's docstring — pass it through so callers can wire it now and
        # get the right behaviour when V3.B implements incremental scrape.
        try:
            # Local import keeps module-level import surface lean and
            # lets tests monkeypatch ``scraper_mod.run_scrape`` cleanly
            # via ``patch.object`` after this module is already imported.
            from opencomputer.skills.profile_scraper import scraper as scraper_mod

            snapshot = scraper_mod.run_scrape(full=full)
        except Exception as exc:  # noqa: BLE001 — never propagate to chat
            return SlashCommandResult(
                output=(
                    f"`/scrape` failed: {type(exc).__name__}: {exc}. "
                    "Check ``opencomputer doctor`` for permission issues."
                ),
                handled=True,
            )

        duration = max(snapshot.ended_at - snapshot.started_at, 0.0)
        snapshot_path = _home() / "profile_scraper" / f"snapshot_{int(snapshot.ended_at)}.json"
        lines = [
            (
                f"Scraped {len(snapshot.facts)} facts from "
                f"{len(snapshot.sources_succeeded)}/{len(snapshot.sources_attempted)} "
                f"sources in {duration:.1f}s."
            ),
            f"Snapshot: {snapshot_path}",
        ]
        # When some sources failed, point that out — easier than asking
        # the user to inspect the file.
        attempted = set(snapshot.sources_attempted)
        succeeded = set(snapshot.sources_succeeded)
        failed = sorted(attempted - succeeded)
        if failed:
            lines.append(f"Failed sources: {', '.join(failed)}")
        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["ScrapeCommand"]
