"""Bootstrap orchestrator — sequences Layers 0/1/2 for a single install run.

Called by the ``opencomputer profile bootstrap`` CLI subcommand. Each
layer is independent and best-effort — a failure in one does not
block subsequent layers.

## V2.A-T1 — F1 consent enforcement on Layer 2 readers

Each Layer 2 ingestion site consults the F1 :class:`ConsentGate`
*before* invoking its reader. If the relevant ``ingestion.*``
capability has been revoked (or never granted at the required tier),
the reader is skipped and a single info-level log line is emitted —
the rest of the bootstrap continues uninterrupted.

Layers 0 and 1 are not gated: Layer 0 reads only system-identity
facts (no third-party data), and Layer 1 stores answers the user
just typed into the interview, which is implicit consent by act of
typing.

The gate is loaded lazily via :func:`_get_consent_gate`. When no
gate is wired (during tests, or in environments where F1 has not been
provisioned) the helper returns ``None`` and every check defaults to
"allowed" — the orchestrator falls back to its pre-V2 behavior so we
do not break first-run installs that have not yet seen the consent
schema. When the gate itself raises, :func:`_consent_allows` catches
and returns ``False`` (fail closed for forensic-trail integrity)
because a misbehaving gate is more dangerous than a missing one.

The browser-extension and messages capabilities are intentionally
NOT enforced here: ``ingestion.browser_extension`` gates the
:class:`BrowserBridgeAdapter` HTTP path which runs in a separate
process, and ``ingestion.messages`` has no reader yet.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opencomputer.profile_bootstrap.identity_reflex import gather_identity
from opencomputer.profile_bootstrap.persistence import (
    write_browser_history_to_graph,
    write_calendar_to_graph,
    write_git_log_to_graph,
    write_identity_to_graph,
    write_interview_answers_to_graph,
    write_recent_files_to_graph,
)
from opencomputer.profile_bootstrap.recent_scan import (
    scan_git_log,
    scan_recent_files,
)
from opencomputer.user_model.store import UserModelStore

_log = logging.getLogger("opencomputer.profile_bootstrap.orchestrator")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Summary of one bootstrap pass for CLI display + audit log.

    V2.A-T5 added ``calendar_events_scanned`` and
    ``browser_visits_scanned`` so the CLI display + JSON marker can
    surface what the calendar / browser-history readers actually saw.
    Before T5 those reads were silent (the result was discarded); now
    every Layer 2 source has a counter, including the consent-denied
    path where the value naturally stays at 0.
    """

    identity_nodes_written: int = 0
    interview_nodes_written: int = 0
    files_scanned: int = 0
    git_commits_scanned: int = 0
    calendar_events_scanned: int = 0
    browser_visits_scanned: int = 0
    # 2026-04-28: Layer 2 writers were unimplemented before this date.
    # Scans returned data, BootstrapResult counted them, but the rows
    # never made it to the user-model graph. These four counters track
    # the actual graph write step per source.
    recent_file_nodes_written: int = 0
    git_nodes_written: int = 0
    calendar_nodes_written: int = 0
    browser_nodes_written: int = 0
    elapsed_seconds: float = 0.0


def _get_consent_gate() -> Any | None:
    """Build (or return) a :class:`ConsentGate` bound to the active profile.

    Returns ``None`` on any failure path — missing F1 schema, missing
    keyring, anything else. Callers must treat ``None`` as "no gate
    configured, allow by default" so that a profile that has never
    been touched by the consent CLI still bootstraps successfully.

    The function is module-level (rather than a closure inside
    :func:`run_bootstrap`) so tests can monkey-patch it via
    ``patch("opencomputer.profile_bootstrap.orchestrator._get_consent_gate")``
    without having to construct a real SQLite DB + keyring.

    Lazy imports keep the orchestrator import-time graph clean — the
    consent module pulls in ``opencomputer.agent.state`` which does
    SQLite migrations, and we don't want that to run at import time
    of the bootstrap path (which is also reachable from CLI doctor /
    plugin loaders).
    """
    try:
        import os
        import sqlite3

        from opencomputer.agent.config import _home
        from opencomputer.agent.consent import (
            AuditLogger,
            ConsentGate,
            ConsentStore,
            KeyringAdapter,
        )
        from opencomputer.agent.state import apply_migrations

        home = _home()
        db_path = home / "sessions.db"
        conn = sqlite3.connect(db_path, check_same_thread=False)
        apply_migrations(conn)

        kr = KeyringAdapter(service="opencomputer-consent", fallback_dir=home)
        key_hex = kr.get("hmac-chain")
        if key_hex is None:
            key_bytes = os.urandom(32)
            kr.set("hmac-chain", key_bytes.hex())
        else:
            key_bytes = bytes.fromhex(key_hex)

        store = ConsentStore(conn)
        audit = AuditLogger(conn, hmac_key=key_bytes)
        return ConsentGate(store=store, audit=audit)
    except Exception:  # noqa: BLE001
        # Any failure → no gate configured. Caller treats as "allow by
        # default". This is the deliberate fallback documented in the
        # module docstring and V2.A-T1 plan; do not tighten it without
        # also updating that documentation.
        _log.debug("consent gate unavailable; ingestion proceeds ungated", exc_info=True)
        return None


def _consent_allows(gate: Any | None, capability_id: str) -> bool:
    """Ask the gate whether ``capability_id`` is currently authorized.

    Returns:
        ``True``  — gate is None (open-by-default fallback) OR gate
                    returned an allow decision OR bypass is active.
        ``False`` — gate denied OR gate raised (fail closed).

    The :class:`CapabilityClaim` is built on the fly from the F1
    taxonomy: ingestion.* capabilities have well-known tier
    requirements (recent_files / git_log are IMPLICIT, calendar /
    browser_history are EXPLICIT). We don't pass a scope because
    bootstrap reads are coarse-grained — the capability is a binary
    "may we touch this source" per the V2.A-T1 plan.
    """
    if gate is None:
        return True

    try:
        # Bypass is treated separately so an emergency-bypass user
        # still sees a single info-level log per skipped site rather
        # than a flood of "denied" entries — the gate.check call
        # would still allow under bypass but the audit chain semantics
        # differ. Honor it explicitly here.
        from opencomputer.agent.consent.bypass import BypassManager
        if BypassManager.is_active():
            return True
    except Exception:  # noqa: BLE001
        pass

    try:
        from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
        from plugin_sdk import CapabilityClaim, ConsentTier

        tier = F1_CAPABILITIES.get(capability_id, ConsentTier.EXPLICIT)
        claim = CapabilityClaim(
            capability_id=capability_id,
            tier_required=tier,
            human_description=f"profile bootstrap reads {capability_id}",
        )
        decision = gate.check(claim, scope=None, session_id=None)
        return bool(getattr(decision, "allowed", False))
    except Exception:  # noqa: BLE001
        # Fail closed — a broken gate must not silently allow data
        # reads. Log so an operator can investigate. The bootstrap
        # continues without this reader; the user can re-run after
        # fixing the gate (or set OPENCOMPUTER_CONSENT_BYPASS=1 to
        # unbrick).
        _log.exception(
            "consent gate raised on check(%s); treating as denied",
            capability_id,
        )
        return False


def run_bootstrap(
    *,
    interview_answers: dict[str, str],
    scan_roots: list[Path],
    git_repos: list[Path],
    include_calendar: bool = True,
    include_browser_history: bool = True,
    store: UserModelStore | None = None,
    marker_path: Path | None = None,
) -> BootstrapResult:
    """Run all MVP bootstrap layers and persist outputs to the user-model graph.

    Marker write at the end is the "bootstrap completed" signal the CLI
    checks on subsequent runs.

    V2.A-T1: each Layer 2 ingestion site is gated on the matching
    ``ingestion.*`` consent capability. A revoked or unset grant
    causes the orchestrator to skip that reader (and log it) without
    failing the rest of the run.
    """
    started = time.monotonic()
    s = store if store is not None else UserModelStore()

    gate = _get_consent_gate()

    # Layer 0 — system-identity facts (not gated; non-third-party data).
    facts = gather_identity()
    identity_n = write_identity_to_graph(facts, store=s)

    # Layer 1 — interview answers (not gated; user just typed them).
    interview_n = write_interview_answers_to_graph(interview_answers, store=s)

    # Layer 2 — files. Gated on ingestion.recent_files.
    files: list = []
    if scan_roots:
        if _consent_allows(gate, "ingestion.recent_files"):
            files = scan_recent_files(roots=scan_roots, days=7)
        else:
            _log.info("Skipping recent_files: consent not granted")

    # Layer 2 — git. Gated on ingestion.git_log.
    commits: list = []
    if git_repos:
        if _consent_allows(gate, "ingestion.git_log"):
            commits = scan_git_log(repo_paths=git_repos, days=7)
        else:
            _log.info("Skipping git_log: consent not granted")

    # Layer 2 — calendar. Gated on ingestion.calendar. Capture the
    # event list so the count surfaces in BootstrapResult; consent
    # denial leaves the local at [] and the counter at 0 naturally.
    calendar_events: list = []
    if include_calendar:
        if _consent_allows(gate, "ingestion.calendar"):
            try:
                from opencomputer.profile_bootstrap.calendar_reader import (
                    read_upcoming_events,
                )
                calendar_events = read_upcoming_events(days=7) or []
            except Exception:  # noqa: BLE001
                _log.exception("calendar read failed")
        else:
            _log.info("Skipping calendar: consent not granted")

    # Layer 2 — browser history. Gated on ingestion.browser_history.
    # Same capture pattern as calendar above. V2.A-T6: scan ALL
    # installed Chromium-family browsers (Chrome / Brave / Edge /
    # Vivaldi / Arc / Chromium) across all their profiles, not just
    # Chrome's Default profile.
    browser_visits: list = []
    if include_browser_history:
        if _consent_allows(gate, "ingestion.browser_history"):
            try:
                from opencomputer.profile_bootstrap.browser_history import (
                    read_all_browser_history,
                )
                browser_visits = read_all_browser_history(days=7) or []
            except Exception:  # noqa: BLE001
                _log.exception("browser history read failed")
        else:
            _log.info("Skipping browser_history: consent not granted")

    # 2026-04-28: persist Layer 2 scans to the user-model graph.
    # Each writer is best-effort — a failure here must not block the
    # rest of the bootstrap, so individual try/except per writer keeps
    # one bad reader from masking the others. ``files`` is the only
    # source that needs the user's home dir for the project-root
    # collapse; the rest are self-contained.
    recent_file_nodes_n = 0
    git_nodes_n = 0
    calendar_nodes_n = 0
    browser_nodes_n = 0

    if files:
        try:
            recent_file_nodes_n = write_recent_files_to_graph(
                files, home=str(Path.home()), store=s,
            )
        except Exception:  # noqa: BLE001 — best-effort write
            _log.exception("write_recent_files_to_graph failed")

    if commits:
        try:
            git_nodes_n = write_git_log_to_graph(commits, store=s)
        except Exception:  # noqa: BLE001
            _log.exception("write_git_log_to_graph failed")

    if calendar_events:
        try:
            calendar_nodes_n = write_calendar_to_graph(calendar_events, store=s)
        except Exception:  # noqa: BLE001
            _log.exception("write_calendar_to_graph failed")

    if browser_visits:
        try:
            browser_nodes_n = write_browser_history_to_graph(browser_visits, store=s)
        except Exception:  # noqa: BLE001
            _log.exception("write_browser_history_to_graph failed")

    elapsed = time.monotonic() - started
    result = BootstrapResult(
        identity_nodes_written=identity_n,
        interview_nodes_written=interview_n,
        files_scanned=len(files),
        git_commits_scanned=len(commits),
        calendar_events_scanned=len(calendar_events),
        browser_visits_scanned=len(browser_visits),
        recent_file_nodes_written=recent_file_nodes_n,
        git_nodes_written=git_nodes_n,
        calendar_nodes_written=calendar_nodes_n,
        browser_nodes_written=browser_nodes_n,
        elapsed_seconds=elapsed,
    )

    if marker_path is not None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({**asdict(result), "completed_at": time.time()}))

    return result


def extract_and_emit_motif(
    *,
    content: str,
    kind: str,
    source_path: str,
    bus: Any | None = None,
) -> bool:
    """Run LLM extraction on an artifact and emit a motif on the F2 bus.

    Returns True if a motif was emitted; False if Ollama is unavailable
    or the extraction was empty. Best-effort — never raises.
    """
    from opencomputer.profile_bootstrap.llm_extractor import (
        ArtifactExtraction,
        ExtractorUnavailableError,
        get_extractor,
    )
    try:
        cfg = _load_config_for_extractor()
        extractor = get_extractor(cfg)
        extraction = extractor.extract(content)
    except ExtractorUnavailableError:
        return False
    if extraction == ArtifactExtraction():
        # All defaults → nothing extracted.
        return False

    if bus is None:
        from opencomputer.ingestion.bus import get_default_bus
        bus = get_default_bus()

    from plugin_sdk.ingestion import SignalEvent

    bus.publish(SignalEvent(
        event_type="layered_awareness.artifact_extraction",
        source="profile_bootstrap.orchestrator",
        metadata={
            "kind": kind,
            "source_path": source_path,
            "topic": extraction.topic,
            "people": list(extraction.people),
            "intent": extraction.intent,
            "sentiment": extraction.sentiment,
            "timestamp": extraction.timestamp,
        },
    ))
    return True


# ─── Cached config loader for the extractor path ─────────────────────


_EXTRACTOR_CONFIG_CACHE: dict[str, Any] = {}


def _load_config_for_extractor() -> Any:
    """Load + cache config for the extractor.

    Cache invalidates on config-file mtime so edits during a long-
    running deepening pass take effect on the next call. Cheap mtime
    stat per call beats re-parsing the YAML for each artifact.
    """
    from opencomputer.agent.config_store import (
        config_file_path,
        load_config,
    )

    path = config_file_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _EXTRACTOR_CONFIG_CACHE.get("entry")
    if cached and cached[0] == mtime:
        return cached[1]
    cfg = load_config(path)
    _EXTRACTOR_CONFIG_CACHE["entry"] = (mtime, cfg)
    return cfg
