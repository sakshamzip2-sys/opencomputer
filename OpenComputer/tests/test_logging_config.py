"""Tests for Round 2B P-4 — centralized rotated logging with session context.

Coverage:

* ContextVar isolation between concurrent coroutines (the whole reason
  we picked ``contextvars`` over ``threading.local``).
* Each :data:`~opencomputer.observability.logging_config._REDACT_PATTERNS`
  entry produces the expected redacted output.
* :class:`~logging.handlers.RotatingFileHandler` actually rotates after
  the configured size threshold.
* Channel routing — INFO records on ``opencomputer`` land in
  ``agent.log``, ``opencomputer.gateway`` records land in
  ``gateway.log``, and ERROR records on ``opencomputer.errors`` land
  in ``errors.log``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from opencomputer.observability.logging_config import (
    RedactingFormatter,
    SessionContextFilter,
    configure,
    set_session_id,
)

# ─── ContextVar isolation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_isolation_between_concurrent_coroutines() -> None:
    """Two ``asyncio.gather`` tasks must each see their own session id.

    This is the load-bearing reason ``contextvars`` was chosen over
    ``threading.local``: asyncio coroutines run on the same OS thread,
    so a thread-local would leak the last-set id into whichever task
    runs second. ContextVar copies on Task creation → no leak.
    """
    barrier_a = asyncio.Event()
    barrier_b = asyncio.Event()
    seen: dict[str, str | None] = {}

    fltr = SessionContextFilter()

    def _stamp(label: str) -> str | None:
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg=label, args=(), exc_info=None,
        )
        fltr.filter(rec)
        return getattr(rec, "session_id", None)

    async def coro_a() -> None:
        set_session_id("sess-A")
        # Yield so coro_b can race ahead and set its own id; if the
        # context were shared, the stamp below would observe sess-B.
        barrier_a.set()
        await barrier_b.wait()
        seen["a"] = _stamp("a-after-yield")

    async def coro_b() -> None:
        await barrier_a.wait()
        set_session_id("sess-B")
        barrier_b.set()
        seen["b"] = _stamp("b-after-set")

    await asyncio.gather(coro_a(), coro_b())

    assert seen["a"] == "sess-A", "coroutine A leaked B's session id"
    assert seen["b"] == "sess-B"


def test_session_id_default_is_dash() -> None:
    """Records emitted outside any session context get ``-``.

    The format string in :func:`configure` references
    ``%(session_id)s`` unconditionally; falling back to ``-`` keeps it
    safe everywhere.
    """
    # ContextVar state can leak across tests in the same coroutine context
    # (and SessionDB.create_session sets it as a side effect since P-4).
    # Reset explicitly so the default-fallback assertion is honest.
    set_session_id(None)
    fltr = SessionContextFilter()
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    fltr.filter(rec)
    assert rec.session_id == "-"


# ─── Redaction ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_substring", "must_not_contain"),
    [
        # Bearer tokens
        (
            "Authorization: Bearer abc123_DEF.456-xyz",
            "Bearer ***",
            "abc123_DEF.456-xyz",
        ),
        # Slack bot tokens (xoxb / xoxp prefixes)
        (
            "slack token: xoxb-1234567890123-1234567890123-AbCdEf01234567",
            "xox?-***",
            "xoxb-1234567890123",
        ),
        (
            "slack user: xoxp-1234567890123-1234567890123-AbCdEf01234567",
            "xox?-***",
            "xoxp-1234567890123",
        ),
        # Telegram bot tokens (digits:base64ish)
        (
            "telegram bot 123456789:AAEfg-HIjklmnop_qrstuvWXYZ012345-aB",
            "***:telegram",
            "123456789:AAEfg-HIjklmnop_qrstuvWXYZ012345-aB",
        ),
        # Anthropic API key
        (
            "key=sk-ant-api03-AbCdEf-12345_xyz",
            "sk-ant-***",
            "sk-ant-api03-AbCdEf-12345_xyz",
        ),
        # Generic OpenAI-style sk- key
        (
            "openai sk-1234567890ABCDEFGHIJ1234",
            "sk-***",
            "sk-1234567890ABCDEFGHIJ1234",
        ),
        # AWS access key id
        (
            "aws access AKIAIOSFODNN7EXAMPLE",
            "AKIA***",
            "AKIAIOSFODNN7EXAMPLE",
        ),
        # Path under <home>/.opencomputer/secrets/
        (
            "loaded /Users/saksham/.opencomputer/secrets/anthropic.json ok",
            "<secret-path>",
            "/Users/saksham/.opencomputer/secrets/anthropic.json",
        ),
    ],
)
def test_redacting_formatter_replaces_each_pattern(
    raw: str, expected_substring: str, must_not_contain: str
) -> None:
    """Every regex in ``_REDACT_PATTERNS`` must produce its replacement."""
    formatter = RedactingFormatter("%(message)s")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg=raw, args=(), exc_info=None,
    )
    out = formatter.format(rec)
    assert expected_substring in out, f"pattern not redacted in: {out!r}"
    assert must_not_contain not in out, f"raw secret leaked into: {out!r}"


def test_redacting_formatter_passes_clean_messages() -> None:
    """Plain log lines without any secret-shaped substring are unchanged."""
    formatter = RedactingFormatter("%(message)s")
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="hello world — no secrets here", args=(), exc_info=None,
    )
    assert formatter.format(rec) == "hello world — no secrets here"


# ─── Rotation ──────────────────────────────────────────────────────────


def test_rotating_file_handler_actually_rotates(tmp_path: Path) -> None:
    """Configure with a tiny ``maxBytes`` and confirm a backup file appears.

    Uses 1 KiB so the test stays fast — we don't need to write 10 MB to
    prove the wiring works. The handler created by :func:`configure`
    inherits all the right knobs (``backupCount=5``,
    ``RotatingFileHandler``); we just shrink the threshold by reaching
    in via the global logger handlers list and re-configuring.
    """
    # Use ``configure`` directly so we exercise the real wiring.
    home = tmp_path / "oc-home"
    configure(home)
    logger = logging.getLogger("opencomputer")
    # Find the just-installed handler for agent.log and shrink its size
    # threshold so the rotation triggers in-test rather than at 10 MB.
    rotating = [
        h for h in logger.handlers
        if h.__class__.__name__ == "RotatingFileHandler"
        and h.baseFilename.endswith("agent.log")
    ]
    assert rotating, "configure() did not install an agent.log handler"
    rotating[-1].maxBytes = 1024  # type: ignore[attr-defined]

    payload = "x" * 200  # 200 bytes per record × 10 records ≈ 2 KiB

    try:
        for i in range(10):
            logger.warning("rotation-test #%d %s", i, payload)
        # Force flush so the size check runs before we inspect the dir.
        for h in logger.handlers:
            h.flush()
    finally:
        # Tear down to avoid bleeding handlers into other tests.
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    logs_dir = home / "logs"
    backups = sorted(logs_dir.glob("agent.log.*"))
    assert backups, f"no rotated backups in {logs_dir}: {list(logs_dir.iterdir())}"


# ─── Channel routing ───────────────────────────────────────────────────


def _drain_handlers(*names: str) -> None:
    """Detach + close every handler on the named loggers."""
    for n in names:
        lg = logging.getLogger(n)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


def test_channel_routing_writes_each_log_file(tmp_path: Path) -> None:
    """ERROR → errors.log; INFO on agent → agent.log; gateway → gateway.log.

    Each tree has its own handler installed by :func:`configure`. We
    emit one record on each and confirm the right file received it.
    Note: log records propagate up the logger hierarchy by default, so
    a record emitted on ``opencomputer.gateway`` lands in BOTH
    ``gateway.log`` (the named handler) AND ``agent.log`` (the parent
    ``opencomputer`` handler). The assertions below allow that.
    """
    home = tmp_path / "oc-home"
    configure(home)

    # Force visibility — root level + the channels we care about.
    logging.getLogger("opencomputer").setLevel(logging.INFO)
    logging.getLogger("opencomputer.gateway").setLevel(logging.INFO)

    try:
        logging.getLogger("opencomputer").info("agent-info-marker")
        logging.getLogger("opencomputer.gateway").info("gateway-info-marker")
        logging.getLogger("opencomputer.errors").error("errors-only-marker")
        for n in ("opencomputer", "opencomputer.gateway", "opencomputer.errors"):
            for h in logging.getLogger(n).handlers:
                h.flush()
    finally:
        _drain_handlers("opencomputer", "opencomputer.gateway", "opencomputer.errors")

    agent_log = (home / "logs" / "agent.log").read_text(encoding="utf-8")
    gateway_log = (home / "logs" / "gateway.log").read_text(encoding="utf-8")
    errors_log = (home / "logs" / "errors.log").read_text(encoding="utf-8")

    assert "agent-info-marker" in agent_log
    assert "gateway-info-marker" in gateway_log
    # ``errors.log`` is filtered to ERROR-level only on the
    # ``opencomputer.errors`` logger — the INFO markers above must not
    # appear there even though their loggers propagate to root.
    assert "errors-only-marker" in errors_log
    assert "agent-info-marker" not in errors_log
    assert "gateway-info-marker" not in errors_log


def test_format_string_carries_session_id(tmp_path: Path) -> None:
    """End-to-end: setting a session id stamps it onto the formatted output."""
    home = tmp_path / "oc-home"
    configure(home)

    set_session_id("sess-format-test")
    try:
        logging.getLogger("opencomputer").setLevel(logging.INFO)
        logging.getLogger("opencomputer").info("integration-marker")
        for h in logging.getLogger("opencomputer").handlers:
            h.flush()
    finally:
        # Reset context for downstream tests.
        set_session_id(None)
        _drain_handlers("opencomputer", "opencomputer.gateway", "opencomputer.errors")

    text = (home / "logs" / "agent.log").read_text(encoding="utf-8")
    assert "[sess-format-test]" in text, text
    assert "integration-marker" in text
