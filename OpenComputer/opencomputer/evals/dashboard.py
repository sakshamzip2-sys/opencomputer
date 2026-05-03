"""Static HTML dashboard renderer for the eval harness.

Reads history.db, builds per-site summary + sparkline points + failing-case
drilldown, renders Jinja2 template to a single self-contained HTML file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opencomputer.evals.history import list_sites_with_history, load_recent_runs

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_dashboard(
    *, db_path: Path, out_path: Path, limit: int = 50, retention: int = 100
) -> Path:
    """Render the dashboard HTML to ``out_path``. Returns out_path."""
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("dashboard.html.j2")

    sites_data = []
    for site_name in list_sites_with_history(db_path):
        rows = load_recent_runs(site_name, db_path=db_path, limit=limit)
        if not rows:
            continue
        latest = rows[0]
        accs = [r["accuracy"] for r in reversed(rows)]
        if len(accs) >= 2:
            n = len(accs)
            spark_points = [
                {
                    "x": round(i * 200 / max(n - 1, 1), 1),
                    "y": round(36 - a * 36, 1),
                }
                for i, a in enumerate(accs)
            ]
        else:
            spark_points = []

        try:
            failing_cases = [
                c for c in json.loads(latest["case_runs_json"]) if not c["correct"]
            ]
        except (json.JSONDecodeError, KeyError, TypeError):
            failing_cases = []

        sites_data.append(
            {
                "name": site_name,
                "latest": latest,
                "spark_points": spark_points,
                "failing_cases": failing_cases,
            }
        )

    html = template.render(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sites=sites_data,
        retention=retention,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
