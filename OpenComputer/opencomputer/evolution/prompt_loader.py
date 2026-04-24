"""Jinja2 environment setup and rendering helpers for evolution prompt templates.

Callers (e.g. ReflectionEngine in B2.3) use ``render_reflect_prompt`` directly
instead of configuring the Jinja2 environment themselves.

Imports stdlib + jinja2 only — no opencomputer.agent or provider dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import jinja2

from opencomputer.evolution.trajectory import SCHEMA_VERSION_CURRENT, TrajectoryRecord

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _make_env() -> jinja2.Environment:
    """Configure the Jinja2 environment for evolution prompt templates."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(_PROMPTS_DIR),
        autoescape=False,  # plain text output — no HTML escaping
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_reflect_prompt(
    records: list[TrajectoryRecord],
    *,
    now_iso: str | None = None,
) -> str:
    """Render the reflection prompt for the given trajectory batch.

    Args:
        records: Completed trajectory records to include in the prompt.  The
            caller is responsible for filtering to completed-only records before
            passing them here.
        now_iso: Current UTC timestamp in ISO 8601 format, used for traceability.
            Defaults to the actual current UTC time when not supplied.  Pass a
            fixed value for deterministic snapshot tests.

    Returns:
        The fully rendered LLM prompt as a plain-text string.
    """
    env = _make_env()
    template = env.get_template("reflect.j2")
    ts = now_iso if now_iso is not None else datetime.now(UTC).isoformat(timespec="seconds")
    return template.render(
        records=records,
        window=len(records),
        now_iso=ts,
        schema_version=SCHEMA_VERSION_CURRENT,
    )
