"""Langfuse backend for ``oc eval`` (Track L2).

Routes ``oc eval run <site> --backend langfuse`` through langfuse's
datasets + ``run_experiment`` API instead of OC's local runner.

On first run for a site, the backend creates the langfuse dataset
``opencomputer-<site>`` and uploads every JSONL case as a dataset
item. Subsequent runs detect the dataset and reuse it. No deletes —
manual curation is via the langfuse UI.

Acceptance: ``oc eval run reflect --backend langfuse`` completes
end-to-end against a self-hosted (or cloud) langfuse instance and
prints the run URL on success.

Falls back gracefully (clear error) when:
- The ``langfuse`` SDK is not importable.
- ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` are unset.
- The langfuse host is unreachable.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencomputer.evals.runner import _resolve_callable
from opencomputer.evals.sites import get_site

logger = logging.getLogger("opencomputer.evals.langfuse")


class LangfuseBackendUnavailableError(RuntimeError):
    """Raised when the langfuse backend can't run (SDK or env missing)."""


def _get_client() -> Any:
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not pub or not sec:
        raise LangfuseBackendUnavailableError(
            "LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY env vars are required. "
            "Run `oc langfuse keys` after `oc langfuse up`, or use "
            "https://cloud.langfuse.com."
        )

    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LangfuseBackendUnavailableError(
            "langfuse SDK is not installed. Run `pip install langfuse`."
        ) from exc

    base_url = os.environ.get(
        "LANGFUSE_BASE_URL", "https://cloud.langfuse.com"
    ).strip()
    return Langfuse(public_key=pub, secret_key=sec, host=base_url)


def _dataset_name(site_name: str) -> str:
    return f"opencomputer-{site_name}"


def _ensure_dataset(client: Any, site_name: str, cases_path: Path) -> None:
    """Create the langfuse dataset + items if they don't exist yet.

    Idempotent — uses langfuse's upsert behaviour. We don't dedupe by
    case content; if a case appears twice across runs (because the
    JSONL was edited between runs), langfuse will store both — that's
    fine, the eval still scores each item independently.
    """
    name = _dataset_name(site_name)
    try:
        existing = client.get_dataset(name)
        if existing is not None:
            return
    except Exception:  # noqa: BLE001 — get_dataset throws on not-found
        pass

    client.create_dataset(
        name=name,
        description=f"OpenComputer eval site: {site_name}",
        metadata={"opencomputer_site": site_name},
    )

    if not cases_path.is_file():
        logger.warning(
            "no JSONL cases at %s — dataset %r is empty", cases_path, name
        )
        return

    n = 0
    for line in cases_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping malformed case line: %r", line[:100])
            continue
        client.create_dataset_item(
            dataset_name=name,
            input=case.get("input", {}),
            expected_output=case.get("expected"),
            metadata={"id": case.get("id"), "rubric_id": case.get("rubric_id")},
        )
        n += 1
    logger.info("uploaded %d items to langfuse dataset %r", n, name)


def _adapter_to_task(callable_: Callable[[dict], Any]) -> Callable:
    """Wrap an OC site adapter as a langfuse-compatible task function.

    Langfuse calls ``task(*, item, **kwargs)`` and expects the task to
    return whatever value the evaluators will score. OC's adapter
    signature is ``adapter(case_input: dict) -> Any``.
    """

    def task(*, item: Any, **_: Any) -> Any:
        case_input = getattr(item, "input", item)
        if not isinstance(case_input, dict):
            return callable_({"value": case_input})
        return callable_(case_input)

    return task


def run_site_via_langfuse(
    *,
    site_name: str,
    cases_dir: Path,
    run_label: str | None = None,
) -> dict[str, Any]:
    """Run a site through langfuse and return a summary dict.

    Returns::

        {
            "backend": "langfuse",
            "site": site_name,
            "dataset": "opencomputer-<site>",
            "run_name": "<label>",
            "run_url": "https://...",
            "experiment": <langfuse experiment object as dict>,
        }
    """
    site = get_site(site_name)
    client = _get_client()
    cases_path = cases_dir / f"{site_name}.jsonl"
    _ensure_dataset(client, site_name, cases_path)

    callable_ = _resolve_callable(site.callable_path)
    task = _adapter_to_task(callable_)

    label = run_label or f"oc-eval-{site_name}"
    dataset = client.get_dataset(_dataset_name(site_name))
    experiment = dataset.run_experiment(name=label, task=task)

    base_url = os.environ.get(
        "LANGFUSE_BASE_URL", "https://cloud.langfuse.com"
    ).strip()
    run_url = (
        getattr(experiment, "url", None)
        or f"{base_url}/datasets/{_dataset_name(site_name)}"
    )

    summary: dict[str, Any] = {
        "backend": "langfuse",
        "site": site_name,
        "dataset": _dataset_name(site_name),
        "run_name": label,
        "run_url": run_url,
    }
    # Best-effort serialize experiment object — varies by SDK version.
    try:
        summary["experiment"] = experiment.dict()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        summary["experiment"] = repr(experiment)

    client.flush()
    return summary
