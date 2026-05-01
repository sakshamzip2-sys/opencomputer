"""Plan 1 of 3 — Profile UI port. Tests the cycle helper + swap consumer.

The persona auto-classifier still runs during Plan 1 (deleted in Plan 2),
so we deliberately leave persona-related runtime state alone.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opencomputer.cli_ui._profile_swap import (
    consume_pending_profile_swap,
    cycle_profile,
    init_active_profile_id,
)


def _runtime() -> SimpleNamespace:
    """Fake RuntimeContext sufficient for the helpers under test."""
    return SimpleNamespace(custom={})


def _seed_profiles(root: Path, names: list[str]) -> None:
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / "profiles" / n).mkdir()


def test_cycle_profile_wraps_through_real_profiles_only(tmp_path, monkeypatch):
    """With real profiles on disk, cycle stays inside them and skips the
    synthetic ``"default"``. UX rule: rendering ``coding → default`` to a
    user who only created real profiles was confusing — see the docstring
    on ``_all_cycle_targets``. The "no profile" state remains reachable
    via ``oc profile use default`` / ``/profile use default``.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    # default → first real (sorted: side, work)
    assert cycle_profile(runtime) == "side"
    assert runtime.custom["pending_profile_id"] == "side"

    runtime.custom["active_profile_id"] = "side"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "work"

    # Wrap-around: from the last real profile back to the first real,
    # NOT to the synthetic "default".
    runtime.custom["active_profile_id"] = "work"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "side"


def test_cycle_profile_default_only_returns_none(tmp_path, monkeypatch):
    """Only the implicit default exists → no other profiles to cycle to."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    assert cycle_profile(runtime) is None
    assert runtime.custom.get("profile_cycle_hint") == (
        "no other profiles — use /profile create"
    )
    assert "pending_profile_id" not in runtime.custom


def test_cycle_profile_unknown_current_starts_from_first(tmp_path, monkeypatch):
    """If active_profile_id is missing/garbage, cycle starts from sorted[0]."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["alpha", "beta"])
    runtime = _runtime()
    # No active_profile_id set.
    assert cycle_profile(runtime) == "alpha"


def test_cycle_profile_re_press_advances_pending(tmp_path, monkeypatch):
    """Pressing Ctrl+P twice without a turn boundary advances pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    cycle_profile(runtime)  # → side
    assert runtime.custom["pending_profile_id"] == "side"

    cycle_profile(runtime)  # → work
    assert runtime.custom["pending_profile_id"] == "work"


def test_consume_swap_no_pending_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    assert consume_pending_profile_swap(runtime) is None


def test_consume_swap_same_as_current_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "work"
    assert consume_pending_profile_swap(runtime) is None
    assert "pending_profile_id" not in runtime.custom


def test_consume_swap_writes_sticky_and_updates_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "work"

    result = consume_pending_profile_swap(runtime)

    assert result == "work"
    assert runtime.custom["active_profile_id"] == "work"
    assert "pending_profile_id" not in runtime.custom
    sticky = (tmp_path / "active_profile").read_text().strip()
    assert sticky == "work"


def test_consume_swap_to_default_clears_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "default"

    result = consume_pending_profile_swap(runtime)

    assert result == "default"
    assert not (tmp_path / "active_profile").exists()


def test_init_active_profile_id_reads_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "work"


def test_init_active_profile_id_default_when_no_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "default"


def test_init_active_profile_id_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "side"  # already set; do not overwrite
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "side"


def test_memory_manager_rebind_to_profile(tmp_path):
    """rebind_to_profile re-resolves the 3 path attributes to a new
    profile home so subsequent read_* calls hit the new files."""
    from opencomputer.agent.memory import MemoryManager

    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    (profile_a).mkdir()
    (profile_b).mkdir()
    (profile_a / "MEMORY.md").write_text("memory-A")
    (profile_a / "USER.md").write_text("user-A")
    (profile_a / "SOUL.md").write_text("soul-A")
    (profile_b / "MEMORY.md").write_text("memory-B")
    (profile_b / "USER.md").write_text("user-B")
    (profile_b / "SOUL.md").write_text("soul-B")

    skills = tmp_path / "skills"
    skills.mkdir()

    mm = MemoryManager(
        declarative_path=profile_a / "MEMORY.md",
        skills_path=skills,
        user_path=profile_a / "USER.md",
        soul_path=profile_a / "SOUL.md",
    )
    assert mm.read_declarative() == "memory-A"
    assert mm.read_user() == "user-A"
    assert mm.read_soul() == "soul-A"

    mm.rebind_to_profile(profile_b)

    assert mm.read_declarative() == "memory-B"
    assert mm.read_user() == "user-B"
    assert mm.read_soul() == "soul-B"


# ---------------------------------------------------------------------------
# Badge rendering tests
# ---------------------------------------------------------------------------

from opencomputer.cli_ui.input_loop import (  # noqa: E402
    _badge_has_meaningful_content,
    _render_mode_badge,
)
from plugin_sdk import RuntimeContext  # noqa: E402


def _runtime_for_badge(**custom):
    return RuntimeContext(custom=dict(custom))


def test_badge_shows_profile_when_set():
    rt = _runtime_for_badge(active_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: work" in text


def test_badge_shows_pending_arrow():
    rt = _runtime_for_badge(active_profile_id="work", pending_profile_id="side")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: work → side" in text


def test_badge_pending_same_as_current_no_arrow():
    rt = _runtime_for_badge(active_profile_id="work", pending_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "→" not in text


def test_badge_default_profile_renders_default():
    rt = _runtime_for_badge(active_profile_id="default")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: default" in text


def test_badge_hint_says_profile_not_persona():
    rt = _runtime_for_badge(active_profile_id="work")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "Ctrl+P profile" in text
    assert "Ctrl+P persona" not in text


def test_badge_hidden_when_profile_uninitialised_and_default_state():
    """Design-intent gate: before init_active_profile_id runs (so no
    active_profile_id key) AND no other axes set, badge returns [].
    Ensures we don't show 'profile: default' forever for new users.
    """
    rt = _runtime_for_badge()  # custom={} — no active_profile_id key
    assert _render_mode_badge(rt) == []


def test_badge_visible_when_profile_initialised_to_default():
    """After init_active_profile_id runs even on default → badge surfaces
    so the user discovers Ctrl+P. Distinguishes 'pre-init' from 'on default'.
    """
    rt = _runtime_for_badge(active_profile_id="default")
    segments = _render_mode_badge(rt)
    text = "".join(t for _, t in segments)
    assert "profile: default" in text


def test_ctrl_p_handler_calls_cycle_profile(tmp_path, monkeypatch):
    """Smoke test: importing input_loop binds Ctrl+P to a function whose
    body references cycle_profile, not _cycle_persona."""
    import inspect

    from opencomputer.cli_ui import input_loop

    src = inspect.getsource(input_loop.read_user_input)
    # The Ctrl+P handler must reference our new helper.
    assert "cycle_profile(runtime" in src
    # And NOT the old persona helper.
    assert "_cycle_persona(runtime" not in src


def test_apply_pending_profile_swap_orchestrator(tmp_path, monkeypatch):
    """Orchestrator: init + consume + rebind memory + evict snapshot."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    # Seed home/ subdirs that profiles.get_profile_dir(name)/"home" expects
    (tmp_path / "profiles" / "work" / "home").mkdir()
    (tmp_path / "profiles" / "work" / "home" / "MEMORY.md").write_text("memory-work")
    (tmp_path / "profiles" / "work" / "home" / "USER.md").write_text("user-work")
    (tmp_path / "profiles" / "work" / "home" / "SOUL.md").write_text("soul-work")

    from opencomputer.agent.loop import _apply_pending_profile_swap
    from opencomputer.agent.memory import MemoryManager

    skills = tmp_path / "skills"
    skills.mkdir()
    home_a = tmp_path / "home_a"
    home_a.mkdir()
    (home_a / "MEMORY.md").write_text("memory-default")
    (home_a / "USER.md").write_text("user-default")
    (home_a / "SOUL.md").write_text("soul-default")
    mm = MemoryManager(
        declarative_path=home_a / "MEMORY.md",
        skills_path=skills,
        user_path=home_a / "USER.md",
        soul_path=home_a / "SOUL.md",
    )

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "work"
    snapshots = {"sid-1": "cached-prompt", "sid-2": "other-cached"}

    swapped = _apply_pending_profile_swap(
        runtime, memory=mm, prompt_snapshots=snapshots, sid="sid-1"
    )

    assert swapped == "work"
    assert runtime.custom["active_profile_id"] == "work"
    assert "pending_profile_id" not in runtime.custom
    assert mm.read_declarative() == "memory-work"
    assert mm.read_soul() == "soul-work"
    assert "sid-1" not in snapshots  # evicted
    assert "sid-2" in snapshots       # other sessions untouched


def test_apply_pending_profile_swap_no_pending_is_noop(tmp_path):
    """No pending → orchestrator is a clean no-op."""
    from opencomputer.agent.loop import _apply_pending_profile_swap
    runtime = _runtime()
    snapshots = {"sid-1": "cached"}
    result = _apply_pending_profile_swap(
        runtime, memory=None, prompt_snapshots=snapshots, sid="sid-1"
    )
    assert result is None
    assert "sid-1" in snapshots  # not evicted on no-op
