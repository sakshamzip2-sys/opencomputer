"""V2.C-T5 — persona auto-classifier wiring into PromptBuilder + AgentLoop.

Three layers of coverage:

1. PromptBuilder rendering: Active persona section appears iff overlay
   is non-empty, and lands between user_facts and skills.
2. Foreground-app detector: graceful "" return when osascript missing.
3. Loop helper: AgentLoop._build_persona_overlay returns a non-empty
   overlay when classifier matches a known persona.
"""
from __future__ import annotations

from unittest.mock import patch

from opencomputer.agent.prompt_builder import PromptBuilder


def test_persona_overlay_rendered_in_prompt():
    """When persona_overlay is set, base.j2 renders an 'Active persona' section."""
    pb = PromptBuilder()
    rendered = pb.build(persona_overlay="User is in coding mode. Be concise.")
    assert "coding mode" in rendered
    assert "Active persona" in rendered


def test_no_overlay_when_empty():
    pb = PromptBuilder()
    rendered = pb.build()
    assert "Active persona" not in rendered


def test_persona_overlay_appears_between_user_facts_and_skills():
    pb = PromptBuilder()
    rendered = pb.build(
        user_facts="- name: Test\n",
        persona_overlay="Persona prompt here.",
    )
    uf_idx = rendered.find("What I know about you")
    po_idx = rendered.find("Active persona")
    sk_idx = rendered.find("Skills available")
    assert uf_idx >= 0 and po_idx >= 0
    assert uf_idx < po_idx
    if sk_idx >= 0:
        assert po_idx < sk_idx


def test_detect_frontmost_app_handles_missing_osascript():
    with patch(
        "opencomputer.awareness.personas._foreground.shutil.which",
        return_value=None,
    ):
        from opencomputer.awareness.personas._foreground import detect_frontmost_app

        assert detect_frontmost_app() == ""


def test_detect_frontmost_app_handles_subprocess_failure():
    """When osascript exists but exits non-zero, return ''."""
    from subprocess import CompletedProcess

    fake_result = CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with (
        patch(
            "opencomputer.awareness.personas._foreground.shutil.which",
            return_value="/usr/bin/osascript",
        ),
        patch(
            "opencomputer.awareness.personas._foreground.subprocess.run",
            return_value=fake_result,
        ),
    ):
        from opencomputer.awareness.personas._foreground import detect_frontmost_app

        assert detect_frontmost_app() == ""


def test_loop_helper_returns_overlay_for_classified_persona(tmp_path, monkeypatch):
    """AgentLoop._build_persona_overlay calls classifier + registry and returns overlay text."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    # Stub-class instance: skip __init__, attach only the attributes
    # _build_persona_overlay touches (just self.db).
    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()

    # Force the classifier to return "coding" by mocking the detector
    # to a known coding-app name, and let the classifier + registry
    # produce a non-empty overlay.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("test-session")
    assert isinstance(overlay, str)
    assert "coding mode" in overlay.lower()


def test_loop_helper_degrades_to_empty_on_classifier_failure(tmp_path, monkeypatch):
    """A classifier exception must NOT break agent startup — return ''."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()

    def _boom(*_a, **_kw):
        raise RuntimeError("classifier exploded")

    with patch(
        "opencomputer.awareness.personas.classifier.classify",
        side_effect=_boom,
    ):
        overlay = loop._build_persona_overlay("test-session")
    assert overlay == ""


# ── Persona-uplift 2026-04-29 — Task 5: override short-circuits ──────


def test_persona_override_short_circuits_classifier(tmp_path, monkeypatch):
    """When runtime.custom['persona_id_override'] is set to a known
    persona id, _build_persona_overlay must return that persona's
    overlay regardless of foreground app or messages."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "companion"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",  # would normally trigger coding
    ):
        overlay = loop._build_persona_overlay("test-session")

    assert (
        "honest answer" in overlay.lower()
        or "warm" in overlay.lower()
        or "companion" in overlay.lower()
    )
    assert loop._active_persona_id == "companion"


def test_persona_override_invalid_id_falls_back_to_classifier(tmp_path, monkeypatch):
    """An override pointing at a deleted/invalid persona id must NOT
    break the loop — fall through to the classifier path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "nonexistent_persona"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("test-session")

    assert isinstance(overlay, str)
    # Falls through to classifier → coding (Cursor is a coding app).
    assert loop._active_persona_id == "coding"


# ── Persona-uplift 2026-04-29 — Task 8: foreground-app cache ─────────


def test_cached_foreground_app_returns_cached_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0

    call_count = {"n": 0}

    def _fake_detect():
        call_count["n"] += 1
        return f"App{call_count['n']}"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        side_effect=_fake_detect,
    ):
        first = loop._cached_foreground_app(now=1000.0)
        second = loop._cached_foreground_app(now=1010.0)  # +10s, within TTL
        third = loop._cached_foreground_app(now=1031.0)  # +31s, past TTL

    assert first == "App1"
    assert second == "App1"  # cached
    assert third == "App2"   # refreshed
    assert call_count["n"] == 2


# ── Persona-uplift 2026-04-29 — Task 9: re-classification ────────────


def _make_loop_with_db(messages):
    """Build a stub AgentLoop with a fixed message history."""
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content
            self.tool_calls = ()

    class _StubDB:
        def __init__(self, msgs):
            self._msgs = msgs

        def get_messages(self, sid):
            return self._msgs

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB([_Msg("user", m) for m in messages])
    loop._runtime = RuntimeContext()
    loop._active_persona_id = "coding"
    loop._active_persona_preferred_tone = ""
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0
    loop._pending_persona_id = ""
    loop._pending_persona_count = 0
    loop._reclassify_calls_since_flip = 999  # no cooldown active
    loop._prompt_snapshots = type(
        "D", (), {"pop": lambda self, k, d=None: None}
    )()
    return loop


def test_reclassify_does_not_flap_on_single_signal(tmp_path, monkeypatch):
    """One emotional message in an otherwise-coding session should NOT
    flip persona on its own. Stability gate requires 2 consecutive
    same-classification turns OR confidence >= 0.85."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(
        ["fix this bug", "i am sad about this regression"]
    )

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    # First sighting of 'companion' — gate not yet passed.
    assert loop._active_persona_id == "coding"
    assert loop._pending_persona_id == "companion"
    assert loop._pending_persona_count == 1


def test_reclassify_flips_after_two_consecutive_signals(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(
        ["fix this bug", "i am sad about this regression"]
    )

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")  # first sighting
        # Second user turn — same classification result.
        loop.db._msgs.append(type(loop.db._msgs[0])(
            "user", "feeling really lonely tonight"
        ))
        loop._maybe_reclassify_persona("test-session")  # second sighting → flip

    assert loop._active_persona_id == "companion"
    assert loop._pending_persona_count == 0  # reset after flip


def test_reclassify_high_confidence_short_circuits_gate(tmp_path, monkeypatch):
    """Confidence >= 0.85 (e.g. trading-app foreground) flips immediately."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    # Trading app -> confidence 0.85 -> immediate flip.
    assert loop._active_persona_id == "trading"


def test_reclassify_skipped_when_override_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["i am sad"])
    loop._runtime.custom["persona_id_override"] = "admin"
    loop._active_persona_id = "admin"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert loop._active_persona_id == "admin"  # unchanged
    assert loop._pending_persona_id == ""      # gate untouched


def test_reclassify_evicts_prompt_snapshot_on_flip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])
    # OrderedDict-shaped pop captures whether eviction happened.
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    # Confidence 0.85 short-circuit -> flip -> snapshot evicted.
    assert "test-session" in evicted


def test_reclassify_honours_persona_dirty_flag_from_slash_command(tmp_path, monkeypatch):
    """When /persona-mode sets _persona_dirty=True, the loop must evict
    the snapshot on the next reclassification call regardless of whether
    persona changed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["fix this bug"])
    loop._runtime.custom["_persona_dirty"] = True
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert "test-session" in evicted
    assert loop._runtime.custom.get("_persona_dirty") is None  # cleared


def test_reclassify_cooldown_prevents_thrashing(tmp_path, monkeypatch):
    """After a flip, refuse to flip again within 3 reclassify calls.
    Prevents thrash when user Cmd-Tabs between coding and trading apps
    in quick succession."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # First flip — Zerodha → trading (immediate, conf 0.85).
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"
    assert loop._reclassify_calls_since_flip == 0

    # User immediately switches back to iTerm — would normally flip to
    # coding but cooldown prevents it.
    loop.db._msgs.append(type(loop.db._msgs[0])("user", "fix this bug"))
    loop._foreground_app_cache = ""  # force refresh
    loop._foreground_app_cache_at = 0.0
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"  # cooldown blocked the flip


def test_reclassify_cooldown_clears_after_threshold(tmp_path, monkeypatch):
    """After 3 reclassify calls, the cooldown lifts and flips can fire."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # Flip to trading.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "trading"

    # Three more reclassify calls in trading mode — cooldown counter
    # increments past 3.
    for _ in range(3):
        with patch(
            "opencomputer.awareness.personas._foreground.detect_frontmost_app",
            return_value="Zerodha Kite",
        ):
            loop._maybe_reclassify_persona("test-session")

    # Now switch app — should flip.
    loop.db._msgs.append(type(loop.db._msgs[0])("user", "fix this bug"))
    loop._foreground_app_cache = ""
    loop._foreground_app_cache_at = 0.0
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._active_persona_id == "coding"


def test_reclassify_dirty_flag_bypasses_cooldown(tmp_path, monkeypatch):
    """Slash-command override (dirty flag) must always evict the snapshot
    even if the cooldown is active. Explicit user choice wins."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["how's the market today"])

    # Flip first to set up cooldown.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")
    assert loop._reclassify_calls_since_flip == 0

    # User runs `/persona-mode admin` mid-cooldown.
    loop._runtime.custom["persona_id_override"] = "admin"
    loop._runtime.custom["_persona_dirty"] = True
    evicted = []

    class _Snap:
        def pop(self, key, default=None):
            evicted.append(key)
            return default

    loop._prompt_snapshots = _Snap()

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Zerodha Kite",
    ):
        loop._maybe_reclassify_persona("test-session")

    assert "test-session" in evicted  # dirty flag evicted despite cooldown


# ── Persona-uplift 2026-04-29 — Task 10: end-to-end acceptance ───────


def test_acceptance_multi_line_first_message_picks_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 1: multi-line first message with greeting
    on a non-first line picks companion, not coding."""
    from opencomputer.awareness.personas.classifier import (
        ClassificationContext,
        classify,
    )

    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source /path/.venv/bin/activate\nhi\nhello",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_acceptance_emotion_message_eventually_flips_to_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 2: starting in coding mode, two
    emotion-shaped turns flips persona to companion."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    loop = _make_loop_with_db(["fix this bug", "i am sad"])

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="iTerm2",
    ):
        loop._maybe_reclassify_persona("acceptance-session")
        assert loop._active_persona_id == "coding"  # gate not yet passed
        loop.db._msgs.append(type(loop.db._msgs[0])(
            "user", "feeling lonely tonight"
        ))
        loop._maybe_reclassify_persona("acceptance-session")

    assert loop._active_persona_id == "companion"


def test_acceptance_persona_mode_override_renders_companion(tmp_path, monkeypatch):
    """Spec acceptance criterion 3: /persona-mode companion forces the
    companion overlay regardless of foreground app."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.runtime_context import RuntimeContext

    class _StubDB:
        def get_messages(self, sid):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()
    loop._runtime = RuntimeContext()
    loop._runtime.custom["persona_id_override"] = "companion"

    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("acceptance-session")

    assert loop._active_persona_id == "companion"
    assert overlay  # non-empty


def test_acceptance_persona_mode_auto_clears_and_reclassifies(tmp_path, monkeypatch):
    """Spec acceptance criterion 4: /persona-mode auto clears the
    override and the classifier resumes."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["persona_id_override"] = "companion"
    asyncio.run(cmd.execute("auto", rt))

    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
    assert rt.custom.get("_persona_dirty") is True


def test_acceptance_persona_mode_rejects_invalid():
    """Spec acceptance criterion 5: /persona-mode <invalid> rejects with
    list of valid ids."""
    import asyncio

    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("definitely_not_a_persona", rt))

    assert "Unknown" in result.output
    assert "companion" in result.output  # available list rendered
    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
