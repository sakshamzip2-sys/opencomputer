"""Batch runner — submit many prompts at once via Anthropic's batch API.

Reads a JSONL of `{"id": str, "prompt": str, "system": str?, "model": str?}`
records, submits them through `messages.batches.create`, polls until done,
writes results to an output JSONL.

Cheaper than serial calls (~50% per Anthropic docs) and runs out-of-band
from the live agent. Useful for: bulk classification, dataset generation,
overnight evaluation runs.

Source: phase-11-commit-list item 11d.2 (Anthropic batches).

Limitations (v1):
- Anthropic only. No batch endpoint shape exists for OpenAI yet (their
  `batches.create` is async-file-based; would be a separate adapter).
- Polls every 30 s; no streaming progress in this iteration.
- Caller must own the API key (we don't read config — keeps batch.py
  importable in test env without a live config).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.batch")

DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """One entry in the input JSONL."""

    id: str
    prompt: str
    system: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One entry in the output JSONL."""

    id: str
    status: str  # "succeeded" | "errored" | "expired" | "canceled"
    output: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def parse_jsonl(path: Path) -> list[BatchRequest]:
    """Read a JSONL file into BatchRequest objects. Raises on malformed lines."""
    requests: list[BatchRequest] = []
    for i, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {i}: not valid JSON: {e}") from e
        if not isinstance(data, dict) or "prompt" not in data:
            raise ValueError(f"line {i}: missing 'prompt' field")
        requests.append(
            BatchRequest(
                id=str(data.get("id", f"req-{i}")),
                prompt=str(data["prompt"]),
                system=str(data.get("system", "")),
                model=str(data.get("model", DEFAULT_MODEL)),
                max_tokens=int(data.get("max_tokens", DEFAULT_MAX_TOKENS)),
            )
        )
    return requests


def _build_anthropic_request(req: BatchRequest) -> dict:
    """Translate a BatchRequest into Anthropic's batch-entry shape."""
    params: dict = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "messages": [{"role": "user", "content": req.prompt}],
    }
    if req.system:
        params["system"] = req.system
    return {"custom_id": req.id, "params": params}


async def submit_batch(requests: list[BatchRequest], *, api_key: str | None = None) -> str:
    """Submit a batch and return its id. Doesn't wait for completion."""
    from opencomputer.agent.anthropic_client import build_anthropic_async_client
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    client = build_anthropic_async_client(resolved_key)
    entries = [_build_anthropic_request(r) for r in requests]
    batch = await client.messages.batches.create(requests=entries)
    return batch.id


async def poll_batch(
    batch_id: str,
    *,
    api_key: str | None = None,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    on_status: callable | None = None,
) -> str:
    """Poll a batch until it leaves the in_progress state. Returns final status."""
    from opencomputer.agent.anthropic_client import build_anthropic_async_client
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    client = build_anthropic_async_client(resolved_key)
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        if on_status:
            on_status(batch.processing_status)
        if batch.processing_status != "in_progress":
            return batch.processing_status
        await asyncio.sleep(interval_s)


async def fetch_results(batch_id: str, *, api_key: str | None = None) -> list[BatchResult]:
    """Fetch all results once a batch is complete."""
    from opencomputer.agent.anthropic_client import build_anthropic_async_client
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    client = build_anthropic_async_client(resolved_key)
    out: list[BatchResult] = []
    async for entry in await client.messages.batches.results(batch_id):
        result_obj = entry.result
        result_type = getattr(result_obj, "type", "errored")
        if result_type == "succeeded":
            msg = result_obj.message
            text_parts: list[str] = []
            for block in msg.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
            out.append(
                BatchResult(
                    id=entry.custom_id,
                    status="succeeded",
                    output="\n".join(text_parts),
                    input_tokens=getattr(msg.usage, "input_tokens", 0),
                    output_tokens=getattr(msg.usage, "output_tokens", 0),
                )
            )
        else:
            err_obj = getattr(result_obj, "error", None)
            err_msg = ""
            if err_obj is not None:
                err_msg = str(getattr(err_obj, "message", err_obj))
            out.append(BatchResult(id=entry.custom_id, status=result_type, error=err_msg))
    return out


def write_results(results: list[BatchResult], path: Path) -> None:
    """Write batch results to a JSONL output file."""
    with path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(
                json.dumps(
                    {
                        "id": r.id,
                        "status": r.status,
                        "output": r.output,
                        "error": r.error,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                    }
                )
                + "\n"
            )


async def run_batch_end_to_end(
    input_path: Path,
    output_path: Path,
    *,
    api_key: str | None = None,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    on_status: callable | None = None,
) -> tuple[str, int]:
    """Convenience: parse → submit → poll → fetch → write. Returns (final_status, count)."""
    requests = parse_jsonl(input_path)
    if not requests:
        raise ValueError(f"{input_path}: no requests found")
    started = time.time()
    batch_id = await submit_batch(requests, api_key=api_key)
    logger.info("batch %s submitted (%d requests)", batch_id, len(requests))
    final_status = await poll_batch(
        batch_id, api_key=api_key, interval_s=interval_s, on_status=on_status
    )
    if final_status not in ("ended", "succeeded"):
        # Anthropic uses "ended" once results are ready (per current SDK).
        # If we ever see a different completion status we still try to fetch.
        logger.warning("batch %s ended with status=%r", batch_id, final_status)
    results = await fetch_results(batch_id, api_key=api_key)
    write_results(results, output_path)
    logger.info(
        "batch %s wrote %d result(s) to %s in %.1fs",
        batch_id,
        len(results),
        output_path,
        time.time() - started,
    )
    return final_status, len(results)


__all__ = [
    "BatchRequest",
    "BatchResult",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_POLL_INTERVAL_S",
    "fetch_results",
    "parse_jsonl",
    "poll_batch",
    "run_batch_end_to_end",
    "submit_batch",
    "write_results",
]
