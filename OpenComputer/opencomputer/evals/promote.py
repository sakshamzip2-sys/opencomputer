"""Atomic promotion of candidate cases into the canonical cases file."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


def promote_candidates(*, site_name: str, cases_dir: Path) -> int:
    """Append <site>.candidates.jsonl onto <site>.jsonl atomically.

    Returns count of promoted cases. Raises ValueError on ID collision —
    leaves both files untouched.
    """
    cases_path = cases_dir / f"{site_name}.jsonl"
    candidates_path = cases_dir / f"{site_name}.candidates.jsonl"

    if not candidates_path.exists():
        return 0

    candidate_lines = [
        line for line in candidates_path.read_text().splitlines() if line.strip()
    ]
    if not candidate_lines:
        return 0

    existing_ids: set[str] = set()
    if cases_path.exists():
        for line in cases_path.read_text().splitlines():
            if line.strip():
                existing_ids.add(json.loads(line)["id"])

    candidate_ids: list[str] = []
    for line in candidate_lines:
        cid = json.loads(line)["id"]
        if cid in existing_ids or cid in candidate_ids:
            raise ValueError(
                f"duplicate case id {cid!r} between candidates and existing cases"
            )
        candidate_ids.append(cid)

    cases_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=cases_dir, prefix=f"{site_name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w") as f:
            if cases_path.exists():
                existing = cases_path.read_text()
                f.write(existing)
                if existing and not existing.endswith("\n"):
                    f.write("\n")
            for line in candidate_lines:
                f.write(line + "\n")
            f.flush()
        shutil.move(str(tmp_path), str(cases_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    candidates_path.unlink()
    return len(candidate_lines)
