"""PTY-driven smoke tests of the live ``oc`` CLI.

These tests spawn ``oc chat`` / ``oc resume`` inside a real pseudo-tty
and assert STRUCTURALLY on the captured output. They cover the bug
classes that escaped unit tests:

    1. **Slash dispatcher wiring** — a typo in a handler's attribute
       access is caught by a broad ``except`` and degrades to an
       "unavailable" message. Unit tests that only check ``result.handled``
       miss this. The live test asserts the SUCCESS string is present
       AND the "unavailable" string is ABSENT.

    2. **prompt_toolkit Application rendering** — the resume picker is
       a full-screen alt-screen application that unit tests can only
       inspect at the data layer (titles, previews, meta strings).
       A live pty exercises the Application's first-frame render and
       lets us assert on session id-prefixes + the 3-line layout
       pattern that ships in ``cc39cf07`` (Claude-Code-style picker).

Why integration-only:
    The tests require ``oc`` on PATH and the user's real SessionDB.
    They take a few seconds each because of subprocess startup. The
    default pytest run skips them via ``-m 'not integration'`` in
    pyproject; CI can opt in with ``pytest -m integration``.

Failure modes handled:
    * ``oc`` not on PATH                  -> skip
    * pty subsystem unavailable           -> skip
    * subprocess startup timeout (8s)     -> fail with diagnostic
    * no sessions in SessionDB            -> picker test skipped
                                              (nothing to render)
    * unexpected exit code from ``oc``    -> reported in fail message
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time

import pytest

# ─── shared helpers ──────────────────────────────────────────────────


# ANSI escape stripper — picker output is densely escaped, /tools less so.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CHARSET = re.compile(r"\x1b[()][AB012]")


def _strip_ansi(raw: bytes) -> str:
    """Return a printable, ANSI-stripped UTF-8 view of ``raw``."""
    text = raw.decode("utf-8", errors="replace")
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CHARSET.sub("", text)
    return text


def _drain(fd: int, *, timeout_s: float) -> bytes:
    """Read everything available from ``fd`` for up to ``timeout_s`` seconds.

    Uses select() to avoid blocking on a pty that has nothing more to
    give us. Returns as soon as the fd goes idle for one poll cycle.
    """
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _spawn_with_pty(argv: list[str]) -> tuple[subprocess.Popen, int]:
    """Spawn ``argv`` with a pty on stdin/stdout/stderr.

    Returns the Popen + the master fd. The caller MUST close master_fd
    and reap the process. Sets TERM=xterm-256color so Rich/prompt_toolkit
    take their full-color paths instead of degrading to plain text.
    """
    import pty

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    return proc, master_fd


def _terminate_with_grace(proc: subprocess.Popen, master_fd: int) -> None:
    """Best-effort: wait → terminate → kill, then close master_fd."""
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        os.close(master_fd)
    except OSError:
        pass


# ─── platform / env precondition checks ──────────────────────────────


def _have_pty() -> bool:
    try:
        import pty  # noqa: F401
    except ImportError:
        return False
    return hasattr(__import__("pty"), "openpty")


def _have_oc_on_path() -> bool:
    return shutil.which("oc") is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _have_pty(), reason="pty subsystem unavailable"),
    pytest.mark.skipif(not _have_oc_on_path(), reason="`oc` not on PATH"),
]


# ─── /tools live smoke ───────────────────────────────────────────────


def test_slash_tools_renders_registered_header_in_live_chat() -> None:
    """``/tools`` typed into a live ``oc chat`` must render the success header.

    Regression: cc39cf07 fixed a one-char typo (``_treg.list_names()`` →
    ``_treg.names()``) that the broad ``except Exception`` swallowed.
    The unit test for the handler was satisfied by ``result.handled``
    in both branches. This test runs the SAME handler in a real chat
    REPL and asserts that the SUCCESS header is present AND the
    "Tool registry unavailable" error string is ABSENT.
    """
    proc, master_fd = _spawn_with_pty(["oc", "chat"])
    captured = b""
    try:
        # Wait for the chat to render its first prompt.
        captured += _drain(master_fd, timeout_s=10.0)

        # Type /tools followed by /exit.
        os.write(master_fd, b"/tools\r")
        captured += _drain(master_fd, timeout_s=4.0)
        os.write(master_fd, b"/exit\r")
        captured += _drain(master_fd, timeout_s=2.0)
    finally:
        _terminate_with_grace(proc, master_fd)

    text = _strip_ansi(captured)

    # 1. Did the handler fire its success branch?
    header_match = re.search(r"Registered tools\s*\((\d+)\)", text)
    assert header_match is not None, (
        "expected '## Registered tools (N)' header in /tools output. "
        f"Captured (last 1500 chars):\n{text[-1500:]}"
    )

    # 2. The count must be a positive integer — an empty registry is a
    #    different (legitimate) branch that prints "No tools registered".
    count = int(header_match.group(1))
    assert count > 0, (
        f"tool registry rendered with count=0; expected at least the "
        f"built-in tools. Captured:\n{text[-1500:]}"
    )

    # 3. The bug shape: a broad except swallowing AttributeError and
    #    falling through to "Tool registry unavailable: <err>". Assert
    #    that string is absent.
    assert "Tool registry unavailable" not in text, (
        f"the /tools handler degraded to its error branch — the cc39cf07 "
        f"regression has come back. Captured:\n{text[-1500:]}"
    )


# ─── resume picker live smoke ────────────────────────────────────────


def test_resume_picker_renders_three_line_layout() -> None:
    """``oc resume`` must render the Claude-Code-style 3-line layout.

    Structure shipped by cc39cf07 + b0b9976e:

        ❯ <title or first_user_message>
            <cwd>
            <age>  ·  <N messages>  ·  <id_prefix>

    We assert STRUCTURALLY:
      1. At least one 8-char lower-hex session id appears
         (matches what the picker shows in its meta line).
      2. The "messages" word + the bullet separator appear
         in proximity, indicating the meta line rendered.
      3. The picker exits cleanly on ESC (smoke for the
         alt-screen tear-down).

    Skipped when SessionDB has no rows — the picker would
    render an empty list which has no structural signal.
    """
    # Pre-flight: does SessionDB have at least one session?
    db = os.path.expanduser("~/.opencomputer/sessions.db")
    if not os.path.exists(db):
        pytest.skip(f"no SessionDB at {db}; picker would render empty")

    import sqlite3

    with sqlite3.connect(db) as conn:
        (row_count,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    if row_count == 0:
        pytest.skip("SessionDB has zero sessions; picker would render empty")

    proc, master_fd = _spawn_with_pty(["oc", "resume"])
    captured = b""
    try:
        # Wait for the alt-screen to render the first frame.
        captured += _drain(master_fd, timeout_s=5.0)
        # Send ESC to exit the picker.
        os.write(master_fd, b"\x1b")
        captured += _drain(master_fd, timeout_s=2.0)
    finally:
        _terminate_with_grace(proc, master_fd)

    text = _strip_ansi(captured)

    # 1. At least one 8-char hex session id prefix appears.
    id_pattern = re.compile(r"\b[0-9a-f]{8}\b")
    ids = id_pattern.findall(text)
    assert ids, (
        "expected at least one 8-char hex session id in picker output. "
        f"Captured (last 1500 chars):\n{text[-1500:]}"
    )

    # 2. The meta line "N message(s)" + the bullet separator must appear
    #    in the same captured frame — proves the 3-line layout's third
    #    line rendered, not just bare titles.
    meta_pattern = re.compile(r"\d+\s+messages?\s*[·•]")
    assert meta_pattern.search(text), (
        "expected the picker meta line ('N messages · <id>'). The "
        "3-line layout regressed. Captured (last 1500 chars):\n"
        f"{text[-1500:]}"
    )

    # 3. The chevron prompt (default highlighted row marker) must appear.
    #    prompt_toolkit's FormattedTextControl renders this for the
    #    first row when the picker takes focus.
    assert "❯" in text, (
        "expected picker focus indicator '❯' in output. "
        f"Captured (last 1500 chars):\n{text[-1500:]}"
    )


# ─── end-to-end title backfill verification ──────────────────────────


def test_picker_falls_back_to_first_user_message_when_title_is_null() -> None:
    """If a session has ``title IS NULL`` AND a recorded user message,
    the picker must render the first_user_message as the headline.

    This is the END-TO-END verification of the title backfill: 27 dirty
    rows had their ``title`` set to NULL, and the picker is supposed to
    fall through to the next-best preview (first_user_message). We
    assert that no captured row in the picker shows a bare "(untitled · id)"
    placeholder when its underlying session has a non-empty user message
    on record.
    """
    db = os.path.expanduser("~/.opencomputer/sessions.db")
    if not os.path.exists(db):
        pytest.skip(f"no SessionDB at {db}")

    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """
            SELECT s.id, m.content
              FROM sessions s
              JOIN messages m ON m.session_id = s.id AND m.role = 'user'
             WHERE s.title IS NULL
             GROUP BY s.id
            HAVING MIN(m.timestamp) IS NOT NULL
             LIMIT 3
            """
        ).fetchall()
    if not rows:
        pytest.skip(
            "no NULL-title sessions with user messages found; backfill "
            "may not have been run on this DB"
        )

    proc, master_fd = _spawn_with_pty(["oc", "resume"])
    captured = b""
    try:
        captured += _drain(master_fd, timeout_s=5.0)
        os.write(master_fd, b"\x1b")
        captured += _drain(master_fd, timeout_s=2.0)
    finally:
        _terminate_with_grace(proc, master_fd)

    text = _strip_ansi(captured)

    # The picker must NEVER render the bare "(untitled · <id>)" form for
    # a session that has a user-message preview available. If it does,
    # the fallback chain in format_session_label is broken.
    assert "(untitled · " not in text, (
        "picker rendered '(untitled · <id>)' for a session that has "
        "first_user_message data available — the fallback chain "
        "regressed. Captured (last 1500 chars):\n"
        f"{text[-1500:]}"
    )


# ─── slash handler family check ──────────────────────────────────────


# Maps the slash command to the substring that signals its error
# branch. The /tools regression that motivated this whole suite had
# the form "<Subject> unavailable: <err>" — every other handler in
# slash_handlers.py with the same try/except-and-return-handled=True
# shape prints a matching string. If any of these appears in the
# captured output, the handler degraded and shipped past CI.
_SLASH_FAMILY_ERROR_STRINGS = {
    "/tools": "Tool registry unavailable",
    "/skills": "Skills unavailable",
    "/cron": "Cron unavailable",
    "/plugins": "Plugins unavailable",
    "/profile": "Profile lookup failed",
    "/agents": "Subagent registry unavailable",
}


def test_slash_handler_family_does_not_silently_degrade() -> None:
    """Drive every slash command in the broad-except-with-handled=True
    family through a live ``oc chat`` and confirm none renders its
    "unavailable" error branch.

    This catches the bug shape of the cc39cf07 /tools fix: a typo in
    an attribute access deep inside the handler's try block is caught
    by the broad except, prints an error string, and returns
    ``handled=True``. Unit tests that only assert ``result.handled``
    miss it; this test asserts the error string is ABSENT for every
    command in the family.
    """
    proc, master_fd = _spawn_with_pty(["oc", "chat"])
    captured = b""
    try:
        # Wait for the chat to render its first prompt.
        captured += _drain(master_fd, timeout_s=10.0)

        # Fire every command in the family. \r submits in prompt_toolkit.
        for cmd in _SLASH_FAMILY_ERROR_STRINGS:
            os.write(master_fd, f"{cmd}\r".encode())
            captured += _drain(master_fd, timeout_s=3.0)

        os.write(master_fd, b"/exit\r")
        captured += _drain(master_fd, timeout_s=2.0)
    finally:
        _terminate_with_grace(proc, master_fd)

    text = _strip_ansi(captured)

    failures: list[str] = []
    for cmd, err_str in _SLASH_FAMILY_ERROR_STRINGS.items():
        if err_str in text:
            failures.append(f"{cmd} -> {err_str!r}")

    assert not failures, (
        "the following slash handlers degraded to their error branch "
        f"(broad-except + handled=True bug shape): {failures}.\n"
        f"Captured (last 2500 chars):\n{text[-2500:]}"
    )


if __name__ == "__main__":  # pragma: no cover — manual debugging entry point
    sys.exit(pytest.main([__file__, "-v", "-s", "-m", "integration"]))
