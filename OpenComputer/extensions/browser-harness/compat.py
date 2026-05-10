"""Hermes -> OC compatibility shims for the browser-harness plugin.

The lifted dispatcher (browser_tool.py), Camofox client (browser_camofox.py),
Camofox identity store (browser_camofox_state.py), and cloud providers
(browser_providers/*) reference Hermes-specific modules:

  - hermes_constants            (get_hermes_home / get_hermes_dir / is_termux)
  - hermes_cli.config           (load_config / read_raw_config)
  - tools.url_safety            (is_safe_url)
  - tools.website_policy        (check_website_access)
  - tools.registry              (registry singleton + tool_error)
  - tools.interrupt             (is_interrupted)
  - tools.tool_backend_helpers  (normalize_browser_cloud_provider)
  - agent.auxiliary_client      (call_llm)

The Nous-managed tool gateway (``tools.managed_tool_gateway`` and
``managed_nous_tools_enabled``) is intentionally NOT shimmed — that
feature is Nous-internal and ``browser_providers/browser_use.py`` has
been edited to drop the dead-code branch. OC users supply their own
``BROWSER_USE_API_KEY`` directly.

This module exposes drop-in replacements that wire the references to OC
equivalents (where they exist) or graceful no-op stubs (where they don't).
The lifted Hermes files only need their `import` statements rewritten to
pull from `compat`; their bodies stay byte-identical to upstream.

Replacement strategy per symbol is documented inline.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path

_log = logging.getLogger("opencomputer.browser_harness.compat")


# =========================================================================
# REAL WIRES — these reach OC equivalents that have the right shape
# =========================================================================


def get_hermes_home() -> Path:
    """Hermes uses ``~/.hermes/`` as its rooted state directory. OC uses the
    active profile's home directory (see ``opencomputer.agent.config._home``).

    Browser-harness uses this for:
      * Camofox identity persistence (``<home>/state/camofox/identity.json``)
      * Per-session screenshot output paths
      * Node-installed-by-Hermes lookup (we skip that path in OC)
    """
    from opencomputer.agent.config import _home

    return _home()


def get_hermes_dir() -> Path:
    """Alias of ``get_hermes_home``. Hermes used both names interchangeably
    across modules; map both to the same OC profile root.
    """
    return get_hermes_home()


def is_safe_url(url: str) -> bool:
    """Wire to OC's existing SSRF / private-IP blocker.

    OC's ``opencomputer.security.url_safety.is_safe_url`` has the same
    contract as Hermes's ``tools.url_safety.is_safe_url``: returns True
    when the URL is safe (public address) and False otherwise.
    """
    from opencomputer.security.url_safety import is_safe_url as _oc_is_safe

    return _oc_is_safe(url)


def read_raw_config() -> dict:
    """Return OC's config as a plain dict.

    Hermes's ``hermes_cli.config.read_raw_config`` returns the YAML loaded
    as a dict; browser-harness uses this to introspect optional keys like
    ``cfg["browser"]["cloud_provider"]``. We serialize OC's typed Config
    dataclass to a dict so dict-style ``.get()`` lookups work.
    """
    from opencomputer.agent.config_store import load_config as _oc_load

    cfg = _oc_load()
    if is_dataclass(cfg):
        try:
            return asdict(cfg)
        except TypeError:
            pass
    # Fallback for non-dataclass shapes (e.g. dicts already, or plain objects)
    if hasattr(cfg, "__dict__"):
        return dict(vars(cfg))
    return {}


def load_config() -> dict:
    """Return OC's config as a plain dict — same shape Hermes's ``load_config`` returns.

    Hermes's ``hermes_cli.config.load_config`` returns a dict (YAML loaded
    via PyYAML). Every browser-harness call site uses dict-style ``.get()``
    access (``load_config().get("browser", {}).get(...)``), so the shim
    matches Hermes's contract by returning a dict — not OC's typed Config
    dataclass. ``load_config`` and ``read_raw_config`` are functionally
    aliases for browser-harness's purposes; both return the same dict.
    """
    return read_raw_config()


# =========================================================================
# NO-OP / NEGATIVE STUBS — features OC does not have yet
# =========================================================================


def is_termux() -> bool:
    """OC does not target the Termux Android shell. Always False."""
    return False


def is_interrupted() -> bool:
    """Cooperative interrupt poll.

    Hermes uses ``tools.interrupt.is_interrupted`` to early-exit long-running
    browser commands when the user presses Ctrl-C / sends ESC. OC's interrupt
    primitive is ``TurnCancelScope`` (``opencomputer.cli_ui.turn_cancel``),
    which lives on the active chat turn.

    Bridge: ``tools.py`` registers a callback into ``_INTERRUPT_PROBES``
    each time it dispatches a tool (the callback closes over the active
    ``TurnCancelScope``). The dispatcher polls THIS function from inside
    ``_run_browser_command``, which fans out to every registered probe
    and returns True if ANY of them signals cancellation.

    Adding a probe is idempotent — same scope registers itself once per
    process. Probes that lose their referent (TurnCancelScope GC'd) are
    auto-pruned. Returns False when no probe is active.
    """
    if not _INTERRUPT_PROBES:
        return False
    for probe in list(_INTERRUPT_PROBES):
        try:
            if probe():
                return True
        except Exception:  # noqa: BLE001 — defensive; never crash the dispatcher
            # Drop bad probes silently so one stale scope doesn't poison
            # the global poll. The owner's __del__ will clean up too.
            try:
                _INTERRUPT_PROBES.discard(probe)
            except Exception:  # noqa: BLE001
                pass
    return False


# Plain set + explicit lifecycle. WeakSet doesn't work here because Python
# creates a fresh bound-method object on each ``obj.method`` access, so a
# weakref drops immediately. The owner (``tools.py``) calls
# ``register_interrupt_probe`` at the start of a tool dispatch and
# ``unregister_interrupt_probe`` in a try/finally at the end.
_INTERRUPT_PROBES: set = set()


def register_interrupt_probe(probe) -> None:
    """Register a zero-arg callable that returns True iff work should stop.

    Called by ``tools.py`` for each browser tool dispatch; the probe closes
    over the active ``TurnCancelScope.is_cancelled`` so the dispatcher's
    polling reaches the right scope.
    """
    _INTERRUPT_PROBES.add(probe)


def unregister_interrupt_probe(probe) -> None:
    """Remove a probe (e.g. when the chat turn ends)."""
    _INTERRUPT_PROBES.discard(probe)


def check_website_access(url: str):
    """No website-access policy layer in browser-harness yet.

    Hermes's ``tools.website_policy.check_website_access`` returns a dict
    ``{"message", "host", "rule", "source"}`` when a URL is blocked by the
    user's allow/deny list, or ``None`` to allow. We always allow for v1;
    OC users get SSRF protection via ``is_safe_url`` and secret-exfil
    protection via the ``redact`` module.
    """
    return None


def normalize_browser_cloud_provider(value) -> str:
    """Lowercase + strip + default the cloud-provider key.

    Mirrors Hermes's
    ``tools.tool_backend_helpers.normalize_browser_cloud_provider``.
    """
    DEFAULT = "browser-use"
    provider = str(value or DEFAULT).strip().lower()
    return provider or DEFAULT


def tool_error(message: str, **fields):
    """Format an error response in Hermes's tool-result shape.

    Hermes's ``tools.registry.tool_error`` returns a JSON-serialisable dict
    in this shape; browser-harness call sites stringify it via ``json.dumps``
    before returning to the agent loop.
    """
    return {"success": False, "error": message, **fields}


# =========================================================================
# AUXILIARY LLM CALL — graceful-degradation stub
# =========================================================================


class CallLLMNotConfigured(NotImplementedError):  # noqa: N818 — matches IsolationFailed/WorktreeNotAvailable convention in sibling extensions
    """Raised by the ``call_llm`` shim until the OC auxiliary system is wired.

    Browser-harness uses ``call_llm`` for two non-critical features:

    * **Vision analysis** — ``browser_tool.py:1925``. Sends a screenshot to
      a vision-capable LLM for visual page understanding. Wrapped in
      ``try/except Exception`` at line 1941 — degrades to "screenshot saved
      but vision analysis unavailable".
    * **Content extraction** — ``browser_tool.py:1137``. Uses an extraction
      LLM to summarise long page snapshots. Wrapped in ``try/except`` at
      line 1141 — degrades to a structure-aware truncation of the raw
      snapshot.

    Both call sites handle this exception cleanly. Wiring this to OC's
    ``opencomputer.agent.auxiliary_client`` is a future enhancement and
    requires shape-matching to the OpenAI-style ``response.choices[0].message.content``
    return shape Hermes expects.
    """


def call_llm(*args, **kwargs):
    """Stub for Hermes's auxiliary LLM client.

    Always raises ``CallLLMNotConfigured``. Both call sites are wrapped in
    ``try/except Exception`` and degrade gracefully (see class docstring).
    """
    raise CallLLMNotConfigured(
        "browser-harness call_llm() is not wired to OC's auxiliary client "
        "yet — vision analysis and content-extraction features fall back "
        "to non-LLM behaviour. See compat.CallLLMNotConfigured for details."
    )


# =========================================================================
# REGISTRY SHIM — Hermes registers tools at import time; OC uses register(api)
# =========================================================================


class _NoOpRegistry:
    """Replacement for Hermes's ``tools.registry.registry`` singleton.

    Hermes's ``browser_tool.py`` registers tool handlers into this global
    at import time (lines 2191+). OC has its own plugin SDK; tool
    registration happens via ``register(api)`` in ``plugin.py``. Make the
    Hermes-shape registry calls into recorded no-ops so the import doesn't
    crash but no double-registration happens.

    Recorded calls are introspectable for diagnostics:

        from extensions.browser_harness.compat import registry
        for entry in registry.recorded_registrations:
            print(entry["name"])
    """

    def __init__(self):
        self.recorded_registrations: list[dict] = []

    def register(self, **kwargs):
        self.recorded_registrations.append(kwargs)


registry = _NoOpRegistry()


__all__ = [
    "CallLLMNotConfigured",
    "call_llm",
    "check_website_access",
    "get_hermes_dir",
    "get_hermes_home",
    "is_interrupted",
    "is_safe_url",
    "is_termux",
    "load_config",
    "normalize_browser_cloud_provider",
    "read_raw_config",
    "register_interrupt_probe",
    "registry",
    "tool_error",
    "unregister_interrupt_probe",
]
