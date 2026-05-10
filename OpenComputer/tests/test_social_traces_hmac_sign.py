"""Phase 6 / Stage 2 — HttpTraceNetworkClient HMAC signing on submit.

When ``submitter_hash`` and ``shared_key`` are both set on the client,
every ``submit()`` request carries:

* ``X-Submitter-Hash: <hash>``
* ``X-Signature: sha256=<hex>`` where ``<hex>`` is
  ``HMAC-SHA256(body_bytes, shared_key).hex()``.

The body bytes used for signing MUST match the bytes-on-wire so the
server can verify. We freeze the JSON serialization
(``json.dumps(payload, separators=(",", ":"))``) on both sides.

Tests use ``httpx.MockTransport`` to capture the outgoing request and
assert on the headers + body.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

# Alias bootstrap (mirrors test_social_traces_http_client.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.client.http" in sys.modules:
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(_ST_DIR)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]

    client_dir = _ST_DIR / "client"
    if "extensions.social_traces.client" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "extensions.social_traces.client",
            str(client_dir / "__init__.py"),
            submodule_search_locations=[str(client_dir)],
        )
        assert spec is not None and spec.loader is not None
        client_pkg = importlib.util.module_from_spec(spec)
        sys.modules["extensions.social_traces.client"] = client_pkg
        client_pkg.__package__ = "extensions.social_traces.client"
        for sub in ("local_file", "http"):
            full = f"extensions.social_traces.client.{sub}"
            if full in sys.modules:
                continue
            init = client_dir / f"{sub}.py"
            sub_spec = importlib.util.spec_from_file_location(full, str(init))
            assert sub_spec is not None and sub_spec.loader is not None
            sub_mod = importlib.util.module_from_spec(sub_spec)
            sub_mod.__package__ = "extensions.social_traces.client"
            sys.modules[full] = sub_mod
            sub_spec.loader.exec_module(sub_mod)
        spec.loader.exec_module(client_pkg)
        parent.client = client_pkg


_ensure_alias()

from extensions.social_traces.client import make_client  # noqa: E402
from extensions.social_traces.client.http import (  # noqa: E402
    HttpTraceNetworkClient,
)

from plugin_sdk.traces import TraceCard, TraceMeta, TraceStep  # noqa: E402


def _make_card() -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent="hmac sign smoke test — long enough to pass validators",
        meta=TraceMeta(
            tags=("homelab", "test"),
            outcome="success",
            token_cost=100,
            loop_count=3,
            harness_version="opencomputer/phase6",
            submitter_hash="a" * 64,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="x",
                result_summary="y",
                duration_ms=10,
            ),
        ),
        distilled_insight="A reasonable insight string for testing.",
        created_at="2026-05-07T00:00:00Z",
    )


# ─── HttpTraceNetworkClient signs when both creds are set ──────────


async def test_submit_unsigned_when_no_creds():
    """Default: no submitter_hash / no shared_key → no signature
    headers. Stage-1 OpenHub deployments accept this."""
    captured: dict[str, httpx.Request] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200, json={"accepted": True, "queue_id": "q-1", "reason": ""}
        )

    transport = httpx.MockTransport(_handler)
    client = HttpTraceNetworkClient(
        endpoint="http://localhost:8000", transport=transport
    )

    receipt = await client.submit(_make_card())
    assert receipt.accepted is True

    req = captured["req"]
    assert "x-submitter-hash" not in {k.lower() for k in req.headers}
    assert "x-signature" not in {k.lower() for k in req.headers}


async def test_submit_signs_when_creds_provided():
    """Both creds set → the request has a valid X-Signature whose
    SHA-256 HMAC matches when computed against the request body."""
    captured: dict[str, httpx.Request] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200, json={"accepted": True, "queue_id": "q-1", "reason": ""}
        )

    transport = httpx.MockTransport(_handler)
    submitter_hash = "deadbeef" * 4
    shared_key = "0123456789abcdef" * 4

    client = HttpTraceNetworkClient(
        endpoint="http://localhost:8000",
        transport=transport,
        submitter_hash=submitter_hash,
        shared_key=shared_key,
    )

    receipt = await client.submit(_make_card())
    assert receipt.accepted is True

    req = captured["req"]
    assert req.headers["X-Submitter-Hash"] == submitter_hash
    sig_header = req.headers["X-Signature"]
    assert sig_header.startswith("sha256=")
    presented_digest = sig_header.split("=", 1)[1]

    expected_digest = hmac.new(
        shared_key.encode("ascii"), req.content, hashlib.sha256
    ).hexdigest()
    assert presented_digest == expected_digest, (
        "X-Signature must HMAC the actual bytes-on-wire so OH server agrees"
    )


async def test_submit_does_not_sign_with_only_hash():
    """``submitter_hash`` alone (no shared_key) is treated as misconfig
    — we send no headers rather than a half-signed request that would
    always fail."""
    captured: dict[str, httpx.Request] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200, json={"accepted": True, "queue_id": "q-1", "reason": ""}
        )

    transport = httpx.MockTransport(_handler)
    client = HttpTraceNetworkClient(
        endpoint="http://localhost:8000",
        transport=transport,
        submitter_hash="deadbeef" * 4,
        shared_key=None,
    )

    await client.submit(_make_card())

    req = captured["req"]
    assert "x-signature" not in {k.lower() for k in req.headers}


async def test_submit_does_not_sign_with_only_key():
    """Symmetric: shared_key alone (no hash) is also misconfig."""
    captured: dict[str, httpx.Request] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200, json={"accepted": True, "queue_id": "q-1", "reason": ""}
        )

    transport = httpx.MockTransport(_handler)
    client = HttpTraceNetworkClient(
        endpoint="http://localhost:8000",
        transport=transport,
        submitter_hash=None,
        shared_key="0123456789abcdef" * 4,
    )

    await client.submit(_make_card())

    req = captured["req"]
    assert "x-signature" not in {k.lower() for k in req.headers}


async def test_submit_body_is_compact_json():
    """The body we sign MUST be the same bytes the server reads. We
    freeze ``json.dumps(separators=(",", ":"))`` so the server's
    ``await request.body()`` returns exactly what we HMAC'd."""
    captured: dict[str, httpx.Request] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(
            200, json={"accepted": True, "queue_id": "q-1", "reason": ""}
        )

    transport = httpx.MockTransport(_handler)
    client = HttpTraceNetworkClient(
        endpoint="http://localhost:8000",
        transport=transport,
        submitter_hash="a" * 32,
        shared_key="b" * 64,
    )

    await client.submit(_make_card())

    body = captured["req"].content
    # JSON must be parseable + compact (no spaces between separators).
    parsed = json.loads(body)
    assert parsed["intent"].startswith("hmac sign smoke test")
    # Compact form: ", " never appears (only ",")
    assert b", " not in body
    assert b": " not in body


# ─── make_client threads HMAC creds through ─────────────────────────


def test_make_client_passes_hmac_creds_to_http_backend(tmp_path: Path):
    """The factory wires ``submitter_hash`` and ``shared_key`` into the
    HttpTraceNetworkClient instance so both submit-side and config-side
    callers go through the same construction path."""
    client = make_client(
        backend="http",
        profile_home=tmp_path,
        endpoint="http://localhost:8000",
        submitter_hash="hash-x",
        shared_key="key-y",
    )
    assert isinstance(client, HttpTraceNetworkClient)
    assert client._submitter_hash == "hash-x"
    assert client._shared_key == "key-y"


def test_make_client_local_backend_ignores_hmac_creds(tmp_path: Path):
    """The local-file backend has no concept of HMAC. Passing creds
    must not raise — they're silently ignored — so a config that
    works for ``backend=http`` doesn't blow up when flipped to local
    for testing."""
    client = make_client(
        backend="local",
        profile_home=tmp_path,
        submitter_hash="hash-x",
        shared_key="key-y",
    )
    # Local backend doesn't expose these — just confirm we didn't raise.
    assert client is not None
