"""Trace network client implementations + factory.

Two implementations live alongside this module:

* :class:`LocalFileTraceNetworkClient` — dev stub. Reads/writes JSON
  under ``<profile_home>/traces/{inbox,outbox}/``. Lets the plugin run
  end-to-end without OpenHub being deployed (and lets a single dev
  machine simulate multiple agents by seeding the inbox with
  hand-curated traces).
* :class:`HttpTraceNetworkClient` — production path. Talks to OpenHub
  (``~/Documents/GitHub/openhub`` sibling repo) over HTTP via
  :mod:`httpx`.

Plugins should not import the concrete classes directly. Use
:func:`make_client` so the choice is config-driven and can be flipped
without code changes.
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.traces import TraceNetworkClient

from .http import HttpTraceNetworkClient
from .local_file import LocalFileTraceNetworkClient


def make_client(
    *,
    backend: str,
    profile_home: Path,
    endpoint: str | None = None,
    submitter_hash: str | None = None,
    shared_key: str | None = None,
) -> TraceNetworkClient:
    """Construct the configured backend.

    Parameters
    ----------
    backend:
        ``"local"`` (dev stub) or ``"http"`` (OpenHub). Other values
        raise ``ValueError``.
    profile_home:
        Path to ``<profile_home>``. The local-file backend stores
        inbox / outbox JSON beneath ``<profile_home>/traces/``.
        Ignored for ``"http"``.
    endpoint:
        Required when ``backend="http"``. Ignored for local. Should
        be a base URL like ``http://127.0.0.1:8000`` or
        ``https://openhub.example.com``; trailing slashes are
        normalized.
    submitter_hash, shared_key:
        Phase 6 / Stage 2 — when both are set, the http backend signs
        every submit with HMAC-SHA256 keyed by ``shared_key`` and sends
        ``X-Submitter-Hash`` + ``X-Signature`` headers. Ignored for
        local. Either or neither is allowed for back-compat with
        OpenHub deployments where ``REQUIRE_HMAC=false``.
    """
    if backend == "local":
        return LocalFileTraceNetworkClient(profile_home=profile_home)
    if backend == "http":
        if not endpoint:
            raise ValueError(
                "social-traces http backend requires an endpoint URL — "
                "set ``social_traces.endpoint`` in config.yaml"
            )
        return HttpTraceNetworkClient(
            endpoint=endpoint,
            submitter_hash=submitter_hash,
            shared_key=shared_key,
        )
    raise ValueError(f"unknown social-traces backend: {backend!r}")


__all__ = ["HttpTraceNetworkClient", "LocalFileTraceNetworkClient", "make_client"]
