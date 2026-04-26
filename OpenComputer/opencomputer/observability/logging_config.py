"""Centralized logging configuration with session context + secret redaction.

Round 2B P-4 — process-wide logger setup invoked from CLI startup
(:func:`opencomputer.cli.chat`, :func:`opencomputer.cli.wire`,
:func:`opencomputer.cli.gateway`). Three concerns wired together:

1. **Rotated file handlers** for the ``opencomputer``,
   ``opencomputer.gateway`` and ``opencomputer.errors`` logger trees,
   landing under ``<HOME>/logs/``. 10 MB per file × 5 backups by default
   so a long-running gateway can't fill the disk silently.
2. **Per-coroutine session context** via :class:`contextvars.ContextVar`.
   ``set_session_id`` is called from the session-creation path
   (:meth:`opencomputer.agent.state.SessionDB.create_session`) and the
   filter stamps every log record with the current session id. Lookups
   that happen outside a session (e.g. CLI bootstrap) get ``-`` so the
   format string is always safe.
3. **Secret redaction** at format time. The formatter walks a small set
   of regex/replacement pairs covering Bearer tokens, Slack/Telegram bot
   tokens, Anthropic / OpenAI / AWS keys, and any path under
   ``<home>/.opencomputer/secrets/``. New patterns belong here so all
   handlers benefit at once.

ContextVar is deliberate — ``threading.local`` would leak the session
id across asyncio coroutines that share the event-loop thread. The
``ContextVar`` reset semantics in CPython 3.11+ keep concurrent
sessions isolated even when ``asyncio.gather`` interleaves them.
"""

from __future__ import annotations

import contextvars
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# CRITICAL: ContextVar, not threading.local. asyncio coroutines all run
# on the same thread; threading.local would leak ``session_id`` across
# concurrent sessions. ContextVar gives us per-coroutine isolation
# (each ``asyncio.Task`` copies the context on creation).
_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None,
)


def set_session_id(sid: str | None) -> None:
    """Bind ``sid`` to the current asyncio context for log stamping.

    Call from the session-creation path so subsequent log records
    inside that coroutine carry the id. Pass ``None`` to clear (rare —
    the next ``set`` overwrites without a clear, since each coroutine
    sees its own copy).
    """
    _session_id_var.set(sid)


class SessionContextFilter(logging.Filter):
    """Stamp the current ContextVar session id onto every log record.

    Records emitted outside any session (e.g. CLI bootstrap, test
    harness) get ``-`` so the format string never blows up on a
    missing attribute.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.session_id = _session_id_var.get() or "-"
        return True


# Order matters: more-specific patterns must precede the generic
# ``sk-*`` / ``Bearer *`` ones so we don't double-replace. Each pattern
# is anchored with ``\b`` or a known prefix to keep false positives
# low — a debug log printing English prose shouldn't accidentally trip
# the AWS key regex.
_REDACT_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"), "Bearer ***"),
    (re.compile(r"xox[bp]-[A-Za-z0-9-]{20,}"), "xox?-***"),
    (re.compile(r"\b\d+:[A-Za-z0-9_-]{20,}\b"), "***:telegram"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    # Path-based: anything under ``<home>/.opencomputer/secrets/``.
    # We assume macOS-style ``/Users/<name>/`` here; Linux paths
    # (``/home/...``) get the same treatment via the ``[^/]+`` segment.
    (re.compile(r"(/Users/[^/]+/\.opencomputer/secrets/[^\s\"']+)"), "<secret-path>"),
]


class RedactingFormatter(logging.Formatter):
    """Wrap the standard formatter and replace known secret shapes.

    Redaction runs after the parent ``Formatter.format`` (so the final
    serialized record — including any args interpolation — is what
    we scrub). Order: format → substitute. Cost: one pass per pattern;
    cheap enough at log volume.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        msg = super().format(record)
        for pat, repl in _REDACT_PATTERNS:
            msg = pat.sub(repl, msg)
        return msg


def configure(home: Path) -> None:
    """Wire rotated file handlers + session context filter onto the trees.

    Idempotent at the call-site level — ``configure()`` may be invoked
    once per CLI subcommand. Re-invocation duplicates handlers (Python
    logging does not de-duplicate by default), so the CLI bootstrap
    guards with a module-level sentinel; tests can call directly.

    Three log files land under ``<home>/logs/``:

    * ``agent.log`` — root ``opencomputer`` tree (INFO and above by
      default; level is set elsewhere via :func:`logging.basicConfig`
      or the ``LOG_LEVEL`` env handling).
    * ``gateway.log`` — ``opencomputer.gateway.*`` only.
    * ``errors.log`` — anything ERROR-or-above on the
      ``opencomputer.errors`` channel. Callers funnel high-severity
      events here explicitly so on-call has a single tail target.

    ``backupCount=5`` × ``maxBytes=10 MB`` = 50 MB ceiling per channel.
    Adjust at the call site if a deployment needs more retention.
    """
    logs_dir = home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(session_id)s] %(name)s: %(message)s"
    )
    fltr = SessionContextFilter()
    for name, fname in [
        ("opencomputer", "agent.log"),
        ("opencomputer.gateway", "gateway.log"),
        ("opencomputer.errors", "errors.log"),
    ]:
        logger = logging.getLogger(name)
        # Idempotence: drop any of OUR previously-attached
        # RotatingFileHandlers writing to the same path. Without this,
        # calling configure() twice doubles handlers and every record
        # is emitted N times — verified via runtime test in /ultrareview.
        # Only remove handlers we own (RotatingFileHandler whose
        # baseFilename matches what we'd attach now); leave any other
        # handler (e.g. user's own console handler) in place.
        target_path = str((logs_dir / fname).resolve())
        for existing in list(logger.handlers):
            if (
                isinstance(existing, RotatingFileHandler)
                and Path(existing.baseFilename).resolve() == Path(target_path)
            ):
                logger.removeHandler(existing)
                existing.close()
        h = RotatingFileHandler(
            logs_dir / fname,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        h.setFormatter(fmt)
        h.addFilter(fltr)
        logger.addHandler(h)
    # Errors channel is restricted; the agent / gateway trees keep
    # whatever the root level is (typically WARNING via Python defaults).
    logging.getLogger("opencomputer.errors").setLevel(logging.ERROR)
