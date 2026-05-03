"""Verification fixture engine.

A ``verify/<name>.json`` fixture is a JSON object with these keys:

  - ``args``       — dict passed to the adapter for the test run
  - ``rowCount``   — ``{"min": int, "max": int}`` bounds on the number
                     of returned rows. Both fields optional.
  - ``columns``    — list[str], expected exact column set in row[0]
  - ``patterns``   — ``{"<col>": "<regex>"}`` per-column regexes
  - ``notEmpty``   — list[str], columns that must be non-empty in
                     every row
  - ``types``      — ``{"<col>": "string|int|float|bool"}`` per-column
                     type expectations

Runs the adapter, then asserts each constraint. Returns a structured
report so the caller (the agent, or CI) can surface a useful diff
when assertions fail. **This is what catches "did the LMS change their
API?" automatically** (BLUEPRINT §11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._decorator import AdapterSpec
from ._runner import run_adapter
from ._site_memory import SiteMemory


@dataclass(slots=True)
class VerifyResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_returned: int = 0
    fixture_path: Path | None = None


async def verify_adapter(
    spec: AdapterSpec,
    *,
    profile_home: Path,
    profile: str | None = None,
    browser_actions: Any | None = None,
    http_client: Any | None = None,
    fixture_override: dict[str, Any] | None = None,
) -> VerifyResult:
    """Load + run the verify fixture for ``spec``.

    Returns a ``VerifyResult`` with the failure list (empty when ok).
    """
    if fixture_override is not None:
        fixture = fixture_override
        fixture_path: Path | None = None
    else:
        memory = SiteMemory.for_site(Path(profile_home), spec.site)
        fixture_path = memory.verify_path(spec.name)
        loaded = memory.read_verify(spec.name)
        if loaded is None:
            return VerifyResult(
                ok=False,
                failures=[f"no verify fixture at {fixture_path}"],
                fixture_path=fixture_path,
            )
        fixture = loaded

    args = dict(fixture.get("args") or {})

    tool_result = await run_adapter(
        spec,
        arguments=args,
        profile_home=profile_home,
        profile=profile,
        browser_actions=browser_actions,
        http_client=http_client,
    )
    failures: list[str] = []
    warnings: list[str] = []
    if tool_result.is_error:
        return VerifyResult(
            ok=False,
            failures=[f"adapter raised: {tool_result.content}"],
            fixture_path=fixture_path,
        )

    # Attempt to parse the stringified payload back into rows.
    rows = _parse_rows(tool_result.content)
    if rows is None:
        return VerifyResult(
            ok=False,
            failures=["adapter output is not parseable as JSON / list[dict]"],
            fixture_path=fixture_path,
        )

    rows_returned = len(rows)

    # rowCount
    rc = fixture.get("rowCount") or {}
    if isinstance(rc, dict):
        if "min" in rc and rows_returned < int(rc["min"]):
            failures.append(
                f"rowCount.min: got {rows_returned}, expected ≥ {rc['min']}"
            )
        if "max" in rc and rows_returned > int(rc["max"]):
            failures.append(
                f"rowCount.max: got {rows_returned}, expected ≤ {rc['max']}"
            )
        if "exact" in rc and rows_returned != int(rc["exact"]):
            failures.append(
                f"rowCount.exact: got {rows_returned}, expected {rc['exact']}"
            )

    # columns — exact match against row[0] keys
    if rows:
        first = rows[0]
        if isinstance(first, dict):
            actual_cols = set(first.keys())
            expected_cols = set(fixture.get("columns") or [])
            if expected_cols:
                missing = expected_cols - actual_cols
                if missing:
                    failures.append(
                        f"columns: missing {sorted(missing)} from {sorted(actual_cols)}"
                    )

        # patterns
        patterns = fixture.get("patterns") or {}
        if isinstance(patterns, dict):
            for col, raw_pat in patterns.items():
                try:
                    rx = re.compile(str(raw_pat))
                except re.error as exc:
                    warnings.append(f"patterns.{col}: invalid regex {raw_pat!r}: {exc}")
                    continue
                for i, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    val = row.get(col)
                    if val is None:
                        continue
                    if not rx.search(str(val)):
                        failures.append(
                            f"patterns.{col}: row {i} value {val!r} does not match {raw_pat!r}"
                        )
                        break

        # notEmpty
        not_empty = fixture.get("notEmpty") or []
        if isinstance(not_empty, list):
            for col in not_empty:
                col = str(col)
                for i, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    val = row.get(col)
                    if val is None or val == "" or val == []:
                        failures.append(
                            f"notEmpty.{col}: row {i} is empty"
                        )
                        break

        # types
        types = fixture.get("types") or {}
        if isinstance(types, dict):
            type_map = {
                "string": str,
                "str": str,
                "int": int,
                "integer": int,
                "float": (int, float),
                "number": (int, float),
                "bool": bool,
                "boolean": bool,
            }
            for col, type_name in types.items():
                py_type = type_map.get(str(type_name).lower())
                if py_type is None:
                    warnings.append(f"types.{col}: unknown type {type_name!r}")
                    continue
                for i, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    val = row.get(col)
                    if val is None:
                        continue
                    if not isinstance(val, py_type):
                        failures.append(
                            f"types.{col}: row {i} value {val!r} is "
                            f"{type(val).__name__}, expected {type_name}"
                        )
                        break

    return VerifyResult(
        ok=not failures,
        failures=failures,
        warnings=warnings,
        rows_returned=rows_returned,
        fixture_path=fixture_path,
    )


def _parse_rows(content: str) -> list[Any] | None:
    """Best-effort parse of the formatted output back into rows."""
    import json

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Common wrapper shapes: ``{"rows": [...]}`` or ``{"data": [...]}``.
        for key in ("rows", "data", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


__all__ = ["VerifyResult", "verify_adapter"]
