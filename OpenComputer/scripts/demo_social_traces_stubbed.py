"""Stubbed-mode demo helper — Phase 10 wire-only verification.

No agent loop, no LLM calls. Talks to a running OpenHub directly via
``HttpTraceNetworkClient`` to confirm the full submit → approve →
query roundtrip works end-to-end with HMAC enforcement on.

Usage:
    python scripts/demo_social_traces_stubbed.py \
        --endpoint http://127.0.0.1:8000 \
        --admin-token $ADMIN_TOKEN \
        --alice-hash $ALICE_HASH --alice-key $ALICE_KEY \
        --bob-hash $BOB_HASH --bob-key $BOB_KEY

Exits 0 on full pass, 1 on any failure with a one-line reason on
stderr. Designed to be CI-friendly.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from urllib import request as urlreq

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    """Stand up ``extensions.social_traces.client.http`` from the
    hyphenated dir without polluting sys.path."""
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
            init = client_dir / f"{sub}.py"
            sub_spec = importlib.util.spec_from_file_location(full, str(init))
            assert sub_spec is not None and sub_spec.loader is not None
            sub_mod = importlib.util.module_from_spec(sub_spec)
            sub_mod.__package__ = "extensions.social_traces.client"
            sys.modules[full] = sub_mod
            sub_spec.loader.exec_module(sub_mod)
        spec.loader.exec_module(client_pkg)
        setattr(parent, "client", client_pkg)


_ensure_alias()

from extensions.social_traces.client.http import HttpTraceNetworkClient  # noqa: E402
from plugin_sdk.traces import TraceCard, TraceMeta, TraceStep  # noqa: E402


def _admin_post(endpoint: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urlreq.Request(
        f"{endpoint}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlreq.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())


def _admin_get(endpoint: str, path: str, token: str) -> dict:
    req = urlreq.Request(
        f"{endpoint}{path}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlreq.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())


def _alice_card(submitter_hash: str) -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=(
            "wire grafana dashboard datasource for the homelab prometheus"
        ),
        meta=TraceMeta(
            tags=("homelab", "grafana", "prometheus"),
            outcome="success",
            token_cost=2400,
            loop_count=6,
            harness_version="opencomputer/demo-stubbed",
            submitter_hash=submitter_hash,
        ),
        steps=(
            TraceStep(
                tool_name="Read",
                arguments_summary="read existing grafana datasources.yaml",
                result_summary="found two stale datasources — pruned the old prom URL",
                duration_ms=120,
            ),
            TraceStep(
                tool_name="Edit",
                arguments_summary="rewrite datasources.yaml with prom URL",
                result_summary="committed the fixed datasource block",
                duration_ms=180,
            ),
            TraceStep(
                tool_name="Bash",
                arguments_summary="kubectl rollout restart grafana",
                result_summary="grafana picked up the new datasource",
                duration_ms=2400,
            ),
        ),
        distilled_insight=(
            "Configure the prometheus datasource block in grafana FIRST, "
            "then rollout-restart so the new query plane is wired before "
            "panels start hitting it. Fixes the empty-panels-on-first-render "
            "rotation problem."
        ),
        created_at="2026-05-07T12:00:00Z",
    )


async def main(args: argparse.Namespace) -> int:
    endpoint = args.endpoint.rstrip("/")
    token = args.admin_token

    print("──────── stubbed demo: alice → submit → approve → bob ────────")

    # Step 1 — alice submits a card via the real HTTP client + HMAC.
    alice_client = HttpTraceNetworkClient(
        endpoint=endpoint,
        submitter_hash=args.alice_hash,
        shared_key=args.alice_key,
    )
    card = _alice_card(args.alice_hash)
    print(f"  [alice] submitting card with tags={card.meta.tags}…")
    receipt = await alice_client.submit(card)
    if not receipt.accepted:
        print(
            f"  [FAIL] alice's submit was rejected: {receipt.reason}",
            file=sys.stderr,
        )
        return 1
    queue_id = receipt.queue_id
    print(f"  [alice] accepted, queue_id={queue_id}")

    # Step 2 — admin approves via REST.
    print(f"  [admin] approving trace {queue_id}…")
    accept_body = {"reason": "demo accept — stubbed mode"}
    accept_resp = _admin_post(
        endpoint, f"/admin/traces/{queue_id}/accept", token, accept_body
    )
    if accept_resp.get("status") != "approved":
        print(
            f"  [FAIL] admin accept did not flip status: {accept_resp}",
            file=sys.stderr,
        )
        return 1
    print(f"  [admin] approved, audit_id={accept_resp.get('audit_id')}")

    # Step 3 — bob queries with overlapping tags + sees alice's card.
    bob_client = HttpTraceNetworkClient(
        endpoint=endpoint,
        submitter_hash=args.bob_hash,
        shared_key=args.bob_key,
    )
    print("  [bob] querying with tags=('grafana', 'prometheus')…")
    result = await bob_client.query(
        intent="my grafana panels are empty after a deploy",
        tags=("grafana", "prometheus"),
        limit=3,
        timeout_s=2.0,
    )
    if not result.traces:
        print("  [FAIL] bob got an empty query result", file=sys.stderr)
        return 1

    found_alice = any(
        "homelab" in t.meta.tags for t in result.traces
    )
    if not found_alice:
        print(
            "  [FAIL] bob's query result didn't include alice's card",
            file=sys.stderr,
        )
        return 1
    top = result.traces[0]
    print(f"  [bob] received {len(result.traces)} trace(s); top id={top.id} score={top.score:.3f}")
    print(f"  [bob] top trace insight: {top.distilled_insight[:80]}...")

    # Step 4 — sanity: alice's card never appeared in /v1/traces/query
    # before approval. Submit a NEW unapproved card and confirm it's
    # not surfaced.
    print("  [paranoid] confirming pending traces are NOT served on query…")
    pending_card = _alice_card(args.alice_hash)
    pending_card_obj = pending_card  # noqa: F841 — kept for clarity
    pending_receipt = await alice_client.submit(pending_card)
    pending_id = pending_receipt.queue_id
    pending_result = await bob_client.query(
        intent="anything",
        tags=("homelab", "grafana", "prometheus"),
        limit=10,
        timeout_s=2.0,
    )
    pending_ids = {t.id for t in pending_result.traces}
    if pending_id in pending_ids:
        print(
            f"  [FAIL] pending trace {pending_id} leaked into query result "
            "before admin approval",
            file=sys.stderr,
        )
        return 1
    print(f"  [paranoid] confirmed: pending trace {pending_id} is NOT served on query")

    print("──────── DEMO PASSED ────────")
    return 0


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--admin-token", required=True)
    p.add_argument("--alice-hash", required=True)
    p.add_argument("--alice-key", required=True)
    p.add_argument("--bob-hash", required=True)
    p.add_argument("--bob-key", required=True)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse())))
