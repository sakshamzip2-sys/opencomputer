"""Baseline save / load / compare for eval reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from opencomputer.evals.runner import RunReport


@dataclass
class BaselineSnapshot:
    site_name: str
    accuracy: float
    parse_failure_rate: float
    timestamp: str
    model: str
    provider: str


@dataclass
class BaselineDiff:
    site_name: str
    accuracy_delta: float
    parse_failure_rate_delta: float
    baseline: BaselineSnapshot
    current_accuracy: float
    current_parse_failure_rate: float


def save_baseline(
    report: RunReport,
    *,
    baselines_dir: Path,
    model: str,
    provider: str,
) -> Path:
    baselines_dir.mkdir(parents=True, exist_ok=True)
    snapshot = BaselineSnapshot(
        site_name=report.site_name,
        accuracy=report.accuracy,
        parse_failure_rate=report.parse_failure_rate,
        timestamp=datetime.now(timezone.utc).isoformat(),
        model=model,
        provider=provider,
    )
    path = baselines_dir / f"{report.site_name}.json"
    path.write_text(json.dumps(asdict(snapshot), indent=2))
    return path


def _load_baseline(baselines_dir: Path, site_name: str) -> BaselineSnapshot | None:
    path = baselines_dir / f"{site_name}.json"
    if not path.exists():
        return None
    return BaselineSnapshot(**json.loads(path.read_text()))


def compare_to_baseline(
    report: RunReport, *, baselines_dir: Path
) -> BaselineDiff | None:
    base = _load_baseline(baselines_dir, report.site_name)
    if base is None:
        return None
    return BaselineDiff(
        site_name=report.site_name,
        accuracy_delta=report.accuracy - base.accuracy,
        parse_failure_rate_delta=report.parse_failure_rate - base.parse_failure_rate,
        baseline=base,
        current_accuracy=report.accuracy,
        current_parse_failure_rate=report.parse_failure_rate,
    )
