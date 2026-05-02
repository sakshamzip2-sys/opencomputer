"""Endpoint exploration via Playwright network interception.

Launches a Playwright session (CDP attach if configured), navigates to
the given URL, and records every network request the site fires into
``endpoints.json``. Used by ``oc browser explore``.

NO LLM is involved here — it's pure observation. The synthesize step
(separate, requires API key) reads endpoints.json and infers
"capabilities" from it.

Privacy note: request headers may contain auth tokens. We REDACT
``Authorization``, ``Cookie``, and ``X-API-Key`` before persisting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_REDACT_HEADERS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
)


@dataclass(slots=True)
class CapturedEndpoint:
    """One observed network call."""

    url: str
    method: str
    status: int
    request_headers: dict[str, str] = field(default_factory=dict)
    response_content_type: str = ""


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace sensitive header values with '<REDACTED>'."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _REDACT_HEADERS:
            out[k] = "<REDACTED>"
        else:
            out[k] = v
    return out


async def explore_endpoints(
    url: str,
    *,
    output_dir: Path,
    max_seconds: float = 10.0,
) -> list[CapturedEndpoint]:
    """Navigate ``url`` and record every fetch, write endpoints.json.

    Returns the list of captured endpoints. Output goes to
    ``<output_dir>/endpoints.json``.

    Honors ``OPENCOMPUTER_BROWSER_CDP_URL`` for CDP attach mode (so the
    user's logins / cookies are used).
    """
    from importlib.util import module_from_spec, spec_from_file_location

    # Lazy-load browser-control to keep this module's deps minimal.
    repo = Path(__file__).resolve().parents[3]
    spec = spec_from_file_location(
        "_browser_control_for_explore",
        str(repo / "extensions" / "browser-control" / "browser.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load browser-control module")
    bc = module_from_spec(spec)
    spec.loader.exec_module(bc)

    output_dir.mkdir(parents=True, exist_ok=True)
    captured: list[CapturedEndpoint] = []

    async with bc._browser_session() as (_browser, context):
        page = await context.new_page()

        async def on_response(resp):
            try:
                req = resp.request
                captured.append(
                    CapturedEndpoint(
                        url=req.url,
                        method=req.method,
                        status=resp.status,
                        request_headers=_redact_headers(dict(req.headers)),
                        response_content_type=resp.headers.get("content-type", ""),
                    )
                )
            except Exception:  # noqa: BLE001 — observation must not crash the explore
                pass

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(max_seconds * 1000))
        except Exception:  # noqa: BLE001 — observation captures whatever fired
            pass

    _write_endpoints_json(output_dir / "endpoints.json", captured)
    return captured


def _write_endpoints_json(path: Path, endpoints: list[CapturedEndpoint]) -> None:
    """Persist captured endpoints as JSON; safe to call with empty list."""
    import json

    data = [
        {
            "url": e.url,
            "method": e.method,
            "status": e.status,
            "request_headers": e.request_headers,
            "response_content_type": e.response_content_type,
        }
        for e in endpoints
    ]
    path.write_text(json.dumps(data, indent=2))
