"""Cross-language integration test — the TypeScript OCWireClient driven
against a live Python WireServer.

The contract test (test_ui_tui_wire_client.py) proves the TS client *names*
every server method. This test proves the client actually *works*: it
builds the client bundle, connects the real OCWireClient to a real
opencomputer.gateway.wire_server, performs the hello handshake, and
invokes RPCs — verifying both halves speak the same wire protocol on the
actual socket.

Skips gracefully when the Node toolchain is unavailable (a CI runner
without Node) — the pure-Python contract test still guards method
coverage there.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TUI_SRC = _REPO / "opencomputer" / "ui-tui" / "src"
_DIST = _REPO / "opencomputer" / "ui-tui" / "dist"
_CLIENT_BUNDLE = _DIST / "wireClient.js"
_RENDER_BUNDLE = _DIST / "renderSmoke.js"
_OVERLAYS_BUNDLE = _DIST / "overlaysSmoke.js"
_MARKDOWN_BUNDLE = _DIST / "markdownSmoke.js"
_EDITOR_BUNDLE = _DIST / "editorSmoke.js"
_HARNESS_BUNDLE = _DIST / "appHarness.js"


def _find_node() -> str | None:
    hit = shutil.which("node")
    if hit:
        return hit
    # nvm installs aren't always on the pytest PATH.
    for base in sorted((Path.home() / ".nvm" / "versions" / "node").glob("v*"), reverse=True):
        cand = base / "bin" / "node"
        if cand.exists():
            return str(cand)
    return None


_NODE = _find_node()


def _ensure_client_bundle() -> bool:
    """Build dist/wireClient.js if missing. Returns True when it exists."""
    if _CLIENT_BUNDLE.is_file():
        return True
    if _NODE is None or not (_TUI_SRC / "node_modules").is_dir():
        return False
    npm = Path(_NODE).with_name("npm")
    try:
        subprocess.run(
            [str(npm), "run", "build"],
            cwd=_TUI_SRC,
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return _CLIENT_BUNDLE.is_file()


pytestmark = pytest.mark.skipif(
    _NODE is None or not _ensure_client_bundle(),
    reason="Node toolchain or built TUI client bundle unavailable",
)

# Node test driver: connect the bundled OCWireClient, handshake, call RPCs,
# print a single JSON result line. argv: [wsUrl, bundleFileUri].
_DRIVER = """
const [wsUrl, bundleUri] = process.argv.slice(1);
import(bundleUri).then(async (m) => {
  const c = new m.OCWireClient(wsUrl);
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('connect timeout')), 6000);
    if (c.connected) { clearTimeout(t); return res(); }
    c.onConnected((ok) => { if (ok) { clearTimeout(t); res(); } });
  });
  const hello = await c.hello();
  const list = await c.sessionsList(5);
  c.close();
  console.log(JSON.stringify({
    ok: true,
    server: hello.server,
    methods: hello.methods.length,
    sessions: list.sessions.length,
  }));
}).catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
  process.exitCode = 1;
});
"""


@pytest.mark.asyncio
async def test_ts_client_round_trips_against_real_wire_server(
    tmp_path: Path,
) -> None:
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session(session_id="itest-session-1", platform="cli", model="m")
    loop = MagicMock(spec=AgentLoop)
    loop.db = db
    server = WireServer(loop=loop, host="127.0.0.1", port=port)
    await server.start()
    assert _NODE is not None  # guaranteed by pytestmark skipif
    try:
        proc = await asyncio.create_subprocess_exec(
            _NODE,
            "--input-type=module",
            "-e",
            _DRIVER,
            f"ws://127.0.0.1:{port}",
            _CLIENT_BUNDLE.as_uri(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=20)
    finally:
        await server.stop()

    out = out_b.decode().strip()
    err = err_b.decode().strip()
    # The driver prints exactly one JSON line; tolerate Node warnings on stderr.
    json_line = next(
        (ln for ln in reversed(out.splitlines()) if ln.startswith("{")), ""
    )
    assert json_line, f"no JSON from node driver.\nstdout={out!r}\nstderr={err!r}"
    result = json.loads(json_line)

    assert result["ok"], f"TS client failed against the wire server: {result}"
    # The handshake genuinely round-tripped — server identity + method list.
    assert result["server"] == "opencomputer"
    assert result["methods"] >= 25, result
    # sessions.list returned the row the test seeded.
    assert result["sessions"] >= 1, result


@pytest.mark.skipif(
    _NODE is None or not _RENDER_BUNDLE.is_file(),
    reason="Node toolchain or built render-smoke bundle unavailable",
)
def test_app_renders_without_crashing() -> None:
    """The Ink <App> component genuinely mounts and produces a frame.

    Typecheck + bundle prove the TUI *compiles*; this proves it *renders*.
    renderSmoke.js mounts <App> via ink-testing-library with a client
    pointed at a dead port and prints the first frame between
    FRAME_START / FRAME_END markers.
    """
    assert _NODE is not None  # guaranteed by skipif
    proc = subprocess.run(
        [_NODE, str(_RENDER_BUNDLE)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout
    assert "FRAME_START" in out and "FRAME_END" in out, (
        f"render-smoke produced no frame.\nstdout={out!r}\nstderr={proc.stderr!r}"
    )
    frame = out.split("FRAME_START", 1)[1].split("FRAME_END", 1)[0]
    # The app's banner + a status line must be in the rendered output.
    assert "OpenComputer TUI" in frame, f"banner missing from frame: {frame!r}"
    assert "disconnected" in frame, f"status line missing from frame: {frame!r}"


@pytest.mark.skipif(
    _NODE is None or not _OVERLAYS_BUNDLE.is_file(),
    reason="Node toolchain or built overlays-smoke bundle unavailable",
)
def test_all_overlays_render() -> None:
    """Every one of the six overlay components mounts and renders its panel.

    overlaysSmoke.js mounts ModelPicker / SkillsHub / Settings / Agents /
    Rollback / Tools with sample data and prints the combined frame.
    """
    assert _NODE is not None  # guaranteed by skipif
    proc = subprocess.run(
        [_NODE, str(_OVERLAYS_BUNDLE)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout
    assert "FRAME_START" in out and "FRAME_END" in out, (
        f"overlays-smoke produced no frame.\nstdout={out!r}\nstderr={proc.stderr!r}"
    )
    frame = out.split("FRAME_START", 1)[1].split("FRAME_END", 1)[0]
    # Each overlay's panel title must appear — proves all six render.
    for title in (
        "Model picker",
        "Skills hub",
        "Settings",
        "Subagents",
        "Checkpoints",
        "Tools",
    ):
        assert title in frame, f"overlay {title!r} missing from frame:\n{frame}"
    # Sample data rows rendered, not just the panel chrome.
    assert "claude-opus-4-7" in frame and "demo-skill" in frame, frame
    # The Spinner widget renders (its label; the braille frame char cycles).
    assert "busychk" in frame, f"Spinner widget did not render:\n{frame}"


@pytest.mark.skipif(
    _NODE is None or not _MARKDOWN_BUNDLE.is_file(),
    reason="Node toolchain or built markdown-smoke bundle unavailable",
)
def test_markdown_renderer() -> None:
    """The streaming-safe markdown renderer renders every construct.

    markdownSmoke.js renders <Markdown> with a sample document; this
    asserts each construct rendered AND that the markdown *syntax* was
    consumed (no raw ``` fences or ** bold markers leak to the screen).
    """
    assert _NODE is not None  # guaranteed by skipif
    proc = subprocess.run(
        [_NODE, str(_MARKDOWN_BUNDLE)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout
    assert "FRAME_START" in out and "FRAME_END" in out, (
        f"markdown-smoke produced no frame.\nstdout={out!r}\nstderr={proc.stderr!r}"
    )
    frame = out.split("FRAME_START", 1)[1].split("FRAME_END", 1)[0]
    for fragment in (
        "Heading One",
        "bold words",
        "inline code",
        "first bullet",
        "const answer = 42",
        "numbered item",
    ):
        assert fragment in frame, f"markdown fragment {fragment!r} missing:\n{frame}"
    # Markdown syntax must be consumed, not shown raw.
    assert "```" not in frame, f"raw code fence leaked to screen:\n{frame}"
    assert "**" not in frame, f"raw bold markers leaked to screen:\n{frame}"


@pytest.mark.skipif(
    _NODE is None or not _EDITOR_BUNDLE.is_file(),
    reason="Node toolchain or built editor-smoke bundle unavailable",
)
def test_multiline_editor() -> None:
    """The multiline editor handles real keystrokes — typed text, a
    Ctrl+N newline, and backspace.

    editorSmoke.js drives the useEditor hook via ink-testing-library's
    stdin: types "hello", Ctrl+N, "world", backspace. The buffer must end
    as two lines ["hello", "worl"].
    """
    assert _NODE is not None  # guaranteed by skipif
    proc = subprocess.run(
        [_NODE, str(_EDITOR_BUNDLE)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout
    assert "FRAME_START" in out and "FRAME_END" in out, (
        f"editor-smoke produced no frame.\nstdout={out!r}\nstderr={proc.stderr!r}"
    )
    frame = out.split("FRAME_START", 1)[1].split("FRAME_END", 1)[0]
    # Typed text landed, the Ctrl+N split it into two lines, backspace
    # dropped the trailing 'd'.
    assert "[hello]" in frame, f"typed line 1 missing:\n{frame}"
    assert "[worl]" in frame, f"line 2 (post-backspace) wrong:\n{frame}"
    assert "rows=2" in frame, f"Ctrl+N newline did not split the buffer:\n{frame}"
    # After "worl" (col 4) the smoke sends two left-arrows → col 2.
    assert "col=2" in frame, f"arrow-key cursor movement broken:\n{frame}"


@pytest.mark.skipif(
    _NODE is None or not _HARNESS_BUNDLE.is_file(),
    reason="Node toolchain or built app-harness bundle unavailable",
)
@pytest.mark.asyncio
async def test_full_app_drives_overlay_against_live_server() -> None:
    """The fully-assembled <App> works end-to-end against a real server.

    This is the integration test the unit smokes don't give: appHarness.js
    mounts the REAL <App>, connects it to a real WireServer, then drives it
    with keystrokes — types "/tools", Enter, ESC — and emits the frame at
    each stage. It exercises the whole glue: mount → WS connect → hello →
    useInput → editor buffer → send() → client-side slash routing →
    openOverlay → a live tools.list RPC → overlay render → ESC close.
    """
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    loop = MagicMock(spec=AgentLoop)
    server = WireServer(loop=loop, host="127.0.0.1", port=port)
    await server.start()
    assert _NODE is not None  # guaranteed by skipif
    try:
        proc = await asyncio.create_subprocess_exec(
            _NODE,
            str(_HARNESS_BUNDLE),
            f"ws://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=30)
    finally:
        await server.stop()

    text = out_b.decode()
    assert "<<END>>" in text, (
        f"app harness did not finish.\nstdout={text!r}\nstderr={err_b.decode()!r}"
    )

    def section(name: str) -> str:
        return text.split(f"<<{name}>>", 1)[1].split("<<", 1)[0]

    connected = section("CONNECTED")
    typed = section("TYPED")
    overlay = section("OVERLAY")
    closed = section("CLOSED")

    # Mount + WS connect + hello handshake landed.
    assert "● connected" in connected, f"app never connected:\n{connected}"
    # Typing "/tools" surfaced the client-side slash palette.
    assert "Slash commands" in typed, f"slash palette did not appear:\n{typed}"
    # Enter routed "/tools" → openOverlay → a live tools.list RPC → the
    # overlay rendered. The RPC resolved (the panel shows either tool rows
    # or the explicit empty state — never a stuck "loading"). The tool
    # *count* depends on what the server process has registered, so this
    # asserts the integrated path, not a specific registry population.
    assert "Tools —" in overlay, f"tools overlay did not open:\n{overlay}"
    # ESC closed it.
    assert "Tools —" not in closed, f"ESC did not close the overlay:\n{closed}"
