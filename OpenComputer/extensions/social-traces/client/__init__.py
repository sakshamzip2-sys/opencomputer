"""Trace network client implementations + factory.

Two implementations live alongside this module:

* :class:`LocalFileTraceNetworkClient` — dev stub. Reads/writes JSON
  under ``<profile_home>/traces/{inbox,outbox}/``. Lets the plugin run
  end-to-end without OpenHub being deployed (and lets a single dev
  machine simulate multiple agents by seeding the inbox with
  hand-curated traces).
* ``HttpTraceNetworkClient`` — landing in Phase 9. Talks to OpenHub
  over HTTPS via :mod:`httpx`.

Plugins should not import the concrete classes directly. Use
:func:`make_client` so the choice is config-driven and can be flipped
without code changes.
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.traces import TraceNetworkClient

from .local_file import LocalFileTraceNetworkClient


def make_client(
    *,
    backend: str,
    profile_home: Path,
    endpoint: str | None = None,
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
    endpoint:
        Required when ``backend="http"``. Ignored for local.
    """
    if backend == "local":
        return LocalFileTraceNetworkClient(profile_home=profile_home)
    if backend == "http":
        raise NotImplementedError(
            "HttpTraceNetworkClient lands in Phase 9. "
            "Use backend: local in social_traces config for now."
        )
    raise ValueError(f"unknown social-traces backend: {backend!r}")


__all__ = ["LocalFileTraceNetworkClient", "make_client"]
