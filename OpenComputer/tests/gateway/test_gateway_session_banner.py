"""A7 — one-line gateway session banner."""
from __future__ import annotations

from unittest.mock import MagicMock

from opencomputer.gateway import dispatch as disp

#: Config that opts the (default-off) banner in.
_BANNER_ON = {"display": {"gateway_banner": {"enabled": True}}}


def _bare_dispatch(display_cfg: dict | None = None) -> disp.Dispatch:
    d = disp.Dispatch.__new__(disp.Dispatch)
    d._banner_shown = set()
    d._display_cfg = _BANNER_ON if display_cfg is None else display_cfg
    return d


def _loop(model: str = "claude-opus-4-7", skills: int = 3) -> MagicMock:
    loop = MagicMock()
    loop.config.model = model
    loop.memory.list_skills = lambda: list(range(skills))
    return loop


def test_banner_has_profile_model_cwd_skills() -> None:
    d = _bare_dispatch()
    banner = d._maybe_session_banner("s1", "default", _loop(), "/tmp/proj")
    assert "OpenComputer" in banner
    assert "profile=default" in banner
    assert "model=claude-opus-4-7" in banner
    assert "cwd=/tmp/proj" in banner
    assert "skills=3" in banner


def test_banner_shown_only_once_per_session() -> None:
    d = _bare_dispatch()
    first = d._maybe_session_banner("s1", "default", _loop(), "/tmp/proj")
    second = d._maybe_session_banner("s1", "default", _loop(), "/tmp/proj")
    assert first != ""
    assert second == ""


def test_banner_suppressed_by_config() -> None:
    d = _bare_dispatch({"display": {"gateway_banner": {"enabled": False}}})
    assert d._maybe_session_banner("s1", "default", _loop(), "/tmp/p") == ""


def test_banner_survives_skill_count_failure() -> None:
    d = _bare_dispatch()
    loop = MagicMock()
    loop.config.model = "m"
    loop.memory.list_skills = MagicMock(side_effect=RuntimeError("boom"))
    banner = d._maybe_session_banner("s1", "default", loop, None)
    # Skill count is decoration — its failure must not lose the banner.
    assert "OpenComputer" in banner
    assert "skills=" not in banner
