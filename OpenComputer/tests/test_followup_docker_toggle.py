"""Follow-up #25 — Docker-toggle detection hint on startup.

If a user runs ``opencomputer setup`` on a Docker-less laptop, the wizard
persists ``provider=""`` and they're stuck on baseline memory forever —
even if Docker is later installed. This hint module prints ONE line per
machine when Docker becomes available, suggesting they re-run
``opencomputer memory setup``.

Design rules (from the follow-up ticket):
  * Hint only when ``cfg.memory.provider == ""`` — don't re-hint if the
    user is already on an overlay.
  * Hint only when Docker *and* compose v2 are both present (both must
    return 0 via subprocess).
  * Hint only once per machine — a sentinel file
    (``_home() / ".docker_toggle_hinted"``) suppresses after the first.
  * Never propagate subprocess failures; this is a UX nicety, must never
    break the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import Config, MemoryConfig

# ─── helpers ────────────────────────────────────────────────────────────


def _make_config(provider: str) -> Config:
    """Build a Config whose ``memory.provider`` matches ``provider``."""
    # MemoryConfig is frozen — build a fresh instance with the target provider.
    mem = MemoryConfig(provider=provider)
    return Config(memory=mem)


class _FakeCompleted:
    """Minimal subprocess.CompletedProcess stand-in — only returncode matters."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _patch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``cli_hints._home`` to ``tmp_path`` so the sentinel is isolated."""
    # Import lazily — the module doesn't exist until we implement it.
    import opencomputer.cli_hints as hints

    monkeypatch.setattr(hints, "_home", lambda: tmp_path)
    return tmp_path


# ─── tests ──────────────────────────────────────────────────────────────


def test_no_hint_when_provider_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If provider is already set (e.g. memory-honcho), no hint — user is
    already on an overlay."""
    _patch_home(tmp_path, monkeypatch)
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    cfg = _make_config(provider="memory-honcho")

    # Subprocess would return success, but we should never get there.
    def _unreachable(*a, **kw):  # noqa: ARG001
        raise AssertionError("subprocess must not run when provider is set")

    monkeypatch.setattr("subprocess.run", _unreachable)

    maybe_print_docker_toggle_hint(cfg)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    # Sentinel must NOT be created — we never evaluated Docker.
    assert not (tmp_path / ".docker_toggle_hinted").exists()


def test_no_hint_when_docker_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Provider empty + Docker not installed → no hint."""
    _patch_home(tmp_path, monkeypatch)
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    cfg = _make_config(provider="")

    # Simulate ``docker --version`` returning non-zero (not installed / broken).
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeCompleted(returncode=127))

    maybe_print_docker_toggle_hint(cfg)

    captured = capsys.readouterr()
    assert captured.out == ""
    # Sentinel must NOT be created — Docker absence is not a final state; if
    # Docker gets installed later we still want to hint on that run.
    assert not (tmp_path / ".docker_toggle_hinted").exists()


def test_hint_prints_once_and_creates_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Provider empty + Docker+compose both available + no sentinel → hint + sentinel.

    Two calls in a row: the second must be silent (sentinel suppresses).
    """
    _patch_home(tmp_path, monkeypatch)
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    cfg = _make_config(provider="")

    # Both subprocess invocations (docker --version, docker compose version)
    # return 0 — stack is ready.
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeCompleted(returncode=0))

    # First call — hint fires.
    maybe_print_docker_toggle_hint(cfg)

    first = capsys.readouterr().out
    assert "Docker is now available" in first
    assert "opencomputer memory setup" in first
    # Sentinel written exactly once.
    sentinel = tmp_path / ".docker_toggle_hinted"
    assert sentinel.exists()

    # Second call in the same process — must NOT print again.
    maybe_print_docker_toggle_hint(cfg)
    second = capsys.readouterr().out
    assert second == ""


def test_no_hint_when_sentinel_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-existing sentinel → no hint even if Docker is present + provider empty."""
    _patch_home(tmp_path, monkeypatch)
    sentinel = tmp_path / ".docker_toggle_hinted"
    sentinel.touch()  # pre-existing

    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    cfg = _make_config(provider="")

    # Subprocess must never run — sentinel short-circuit comes first.
    def _unreachable(*a, **kw):  # noqa: ARG001
        raise AssertionError("subprocess must not run when sentinel exists")

    monkeypatch.setattr("subprocess.run", _unreachable)

    maybe_print_docker_toggle_hint(cfg)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_hint_swallows_docker_detection_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If subprocess raises (OSError, TimeoutExpired, anything), the hint
    function swallows it — no stderr propagation, no sentinel created."""
    _patch_home(tmp_path, monkeypatch)
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    cfg = _make_config(provider="")

    def _explode(*a, **kw):  # noqa: ARG001
        raise OSError("boom — docker binary missing or locked")

    monkeypatch.setattr("subprocess.run", _explode)

    # Must NOT raise.
    maybe_print_docker_toggle_hint(cfg)

    captured = capsys.readouterr()
    assert captured.out == ""
    # Stderr should be empty too — we swallow silently.
    assert captured.err == ""
    # Sentinel must NOT be created — detection didn't succeed, so we want
    # another chance on the next run.
    assert not (tmp_path / ".docker_toggle_hinted").exists()
