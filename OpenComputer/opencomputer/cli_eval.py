"""'oc eval' subcommand: run, generate, regress, history, dashboard, promote."""

from __future__ import annotations

import json as _json
import os
from pathlib import Path

import typer

from opencomputer.evals.baseline import compare_to_baseline, save_baseline
from opencomputer.evals.report import format_report, format_report_json
from opencomputer.evals.runner import run_site
from opencomputer.evals.sites import SITES, get_site

eval_app = typer.Typer(help="Run, generate, and regress LLM-decision-point evals.")


def _default_history_db() -> Path:
    """Resolve the history DB path from env or repo-default at call time.

    Looking up the env var lazily means tests can monkeypatch it AFTER
    typer parsed defaults at import time.
    """
    return Path(os.environ.get("OPENCOMPUTER_EVAL_HISTORY_DB", "evals/history.db"))


@eval_app.command("run")
def run_command(
    site: str = typer.Argument(..., help="Site name. 'all' to run every registered site."),
    save_baseline_flag: bool = typer.Option(
        False, "--save-baseline", help="Persist this run's accuracy as the new baseline."
    ),
    cases_dir: Path = typer.Option(
        Path("evals/cases"), "--cases-dir", help="Directory containing <site>.jsonl files."
    ),
    baselines_dir: Path = typer.Option(
        Path("evals/baselines"), "--baselines-dir", help="Directory for baseline snapshots."
    ),
    grader_model: str | None = typer.Option(
        None,
        "--grader-model",
        help="Explicit model for rubric grader (required for non-Anthropic setups).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show failing case details (input/expected/actual/error)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of formatted text."
    ),
    case_id: list[str] | None = typer.Option(
        None, "--case-id", help="Filter to specific case ID(s). Repeatable."
    ),
    no_history: bool = typer.Option(
        False, "--no-history", help="Skip writing run to SQLite history."
    ),
    history_db: Path | None = typer.Option(
        None, "--history-db", help="Override history DB path."
    ),
):
    """Run evals for one or all sites."""
    target_sites = list(SITES) if site == "all" else [site]
    json_payloads: list[dict] = []

    for s in target_sites:
        eval_site = get_site(s)  # validates name; raises if unknown

        grader_provider = None
        if eval_site.grader == "rubric":
            try:
                from opencomputer.evals.providers import get_grader_provider

                grader_provider = get_grader_provider(model_override=grader_model)
            except (ImportError, RuntimeError) as e:
                typer.echo(
                    f"Skipping rubric site {s!r} — grader provider unavailable: {e}",
                    err=True,
                )
                continue

        report = run_site(
            site_name=s,
            cases_dir=cases_dir,
            grader_provider=grader_provider,
            case_ids=case_id,
        )
        diff = compare_to_baseline(report, baselines_dir=baselines_dir)

        if json_output:
            json_payloads.append(_json.loads(format_report_json(report, baseline_diff=diff)))
        else:
            typer.echo(format_report(report, baseline_diff=diff, verbose=verbose))

        # Write to history (after grading, before any baseline save).
        if not no_history:
            from opencomputer.evals.history import record_run

            record_run(
                report,
                db_path=history_db or _default_history_db(),
                model="claude-sonnet-4-6",
                provider="anthropic",
                grader_model=grader_model,
            )

        if save_baseline_flag:
            save_baseline(
                report,
                baselines_dir=baselines_dir,
                model="claude-sonnet-4-6",
                provider="anthropic",
            )

    if json_output:
        if len(json_payloads) == 1:
            typer.echo(_json.dumps(json_payloads[0], indent=2, default=str))
        else:
            typer.echo(_json.dumps({"sites": json_payloads}, indent=2, default=str))


@eval_app.command("generate")
def generate_command(
    site: str = typer.Argument(...),
    n: int = typer.Option(30, "-n", help="Number of cases to generate."),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
    grader_model: str | None = typer.Option(
        None, "--grader-model", help="Override the model used to generate cases."
    ),
):
    """Generate test cases via LLM. Writes to <site>.candidates.jsonl."""
    from opencomputer.evals.generator import generate_cases

    try:
        from opencomputer.evals.providers import get_grader_provider
    except ImportError:
        typer.echo(
            "Generator provider helper not available. "
            "evals/providers.py is added in Task 1.12.",
            err=True,
        )
        raise typer.Exit(code=2)

    provider = get_grader_provider(model_override=grader_model)
    out_path = generate_cases(
        site_name=site, n=n, cases_dir=cases_dir, generator_provider=provider
    )
    typer.echo(f"Generated candidates -> {out_path}")


@eval_app.command("regress")
def regress_command(
    site: str = typer.Argument("all"),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
    baselines_dir: Path = typer.Option(Path("evals/baselines"), "--baselines-dir"),
    grader_model: str | None = typer.Option(
        None,
        "--grader-model",
        help="Explicit model for rubric grader (required to regress rubric-graded sites).",
    ),
):
    """Run sites and exit non-zero if any accuracy regressed past site's threshold.

    Each EvalSite carries its own ``regression_threshold`` (default 0.05).
    Rubric-graded sites are skipped if no grader provider is wired.
    """
    target_sites = list(SITES) if site == "all" else [site]
    regressed = []
    skipped: list[tuple[str, str]] = []

    for s in target_sites:
        eval_site = get_site(s)
        threshold = eval_site.regression_threshold

        grader_provider = None
        if eval_site.grader == "rubric":
            try:
                from opencomputer.evals.providers import get_grader_provider

                grader_provider = get_grader_provider(model_override=grader_model)
            except (ImportError, RuntimeError) as e:
                skipped.append((s, str(e)))
                continue

        report = run_site(
            site_name=s,
            cases_dir=cases_dir,
            grader_provider=grader_provider,
        )
        diff = compare_to_baseline(report, baselines_dir=baselines_dir)
        if diff is not None and diff.accuracy_delta < -threshold:
            regressed.append((s, diff.accuracy_delta))

    for s, reason in skipped:
        typer.echo(f"Skipped rubric site {s!r}: {reason}", err=True)

    if regressed:
        typer.echo("REGRESSED:")
        for s, delta in regressed:
            typer.echo(f"  {s}: {delta:+.2%}")
        raise typer.Exit(code=1)

    typer.echo("No regressions detected.")


@eval_app.command("history")
def history_command(
    site: str = typer.Argument("all"),
    limit: int = typer.Option(20, "--limit"),
    history_db: Path | None = typer.Option(None, "--history-db"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Show recent runs from SQLite history."""
    from opencomputer.evals.history import list_sites_with_history, load_recent_runs

    db_path = history_db or _default_history_db()
    sites = list_sites_with_history(db_path) if site == "all" else [site]

    output_rows: list[dict] = []
    for s in sites:
        rows = load_recent_runs(s, db_path=db_path, limit=limit)
        output_rows.extend(rows)

    if json_output:
        typer.echo(_json.dumps(output_rows, indent=2, default=str))
        return

    if not output_rows:
        typer.echo("No history yet. Run 'oc eval run all' first.")
        return

    for r in output_rows:
        typer.echo(
            f"{r['timestamp'][:19]}  {r['site_name']:<25}  "
            f"{r['correct']}/{r['total']} ({r['accuracy']:.1%})  "
            f"infra={r['infra_failures']}  parse={r['parse_failures']}"
        )


@eval_app.command("dashboard")
def dashboard_command(
    out: Path = typer.Option(Path("evals/dashboard/index.html"), "--out"),
    limit: int = typer.Option(50, "--limit"),
    history_db: Path | None = typer.Option(None, "--history-db"),
):
    """Render a static HTML dashboard of run history."""
    from opencomputer.evals.dashboard import render_dashboard

    db_path = history_db or _default_history_db()
    render_dashboard(db_path=db_path, out_path=out, limit=limit)
    typer.echo(f"Dashboard written to {out}")


@eval_app.command("promote")
def promote_command(
    site: str = typer.Argument(...),
    cases_dir: Path = typer.Option(Path("evals/cases"), "--cases-dir"),
):
    """Atomically merge <site>.candidates.jsonl into <site>.jsonl."""
    from opencomputer.evals.promote import promote_candidates

    n = promote_candidates(site_name=site, cases_dir=cases_dir)
    if n == 0:
        typer.echo(f"No candidates to promote for {site}.")
    else:
        typer.echo(f"Promoted {n} case{'s' if n != 1 else ''} for {site}.")
