"""Tests for wake-word custom training pipeline.

Spec: docs/superpowers/specs/2026-05-07-wake-word-custom-training-design.md
Plan: docs/superpowers/plans/2026-05-07-wake-word-custom-training.md
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Phase 1.3 — doctor probe
# ---------------------------------------------------------------------------


def test_doctor_wake_train_info_when_deps_missing(monkeypatch):
    """When training deps are missing, doctor reports 'info' (not error)."""
    # Force ImportError for each training dep so we get the info path.
    for mod in (
        "torch", "openwakeword.train", "huggingface_hub", "soundfile", "piper",
    ):
        monkeypatch.setitem(sys.modules, mod, None)
    from opencomputer.doctor import _check_wake_train_capable

    result = _check_wake_train_capable()
    assert result.level == "info"
    assert result.ok is True
    assert "wake-train deps missing" in result.message


def test_doctor_wake_train_ok_when_all_deps_present(monkeypatch):
    """When all deps importable, doctor reports level=info + 'ready'."""
    # `__import__("openwakeword.train")` walks the package chain — both
    # `openwakeword` and `openwakeword.train` must be in sys.modules.
    for mod in (
        "torch", "openwakeword", "openwakeword.train",
        "huggingface_hub", "soundfile", "piper",
    ):
        monkeypatch.setitem(sys.modules, mod, types.ModuleType(mod))
    from opencomputer.doctor import _check_wake_train_capable

    result = _check_wake_train_capable()
    assert result.ok is True
    assert result.level == "info"
    assert "ready" in result.message


# ---------------------------------------------------------------------------
# Phase 3.1 — TrainConfig / TrainResult / ensure_deps
# ---------------------------------------------------------------------------


def test_train_config_defaults(tmp_path):
    from opencomputer.voice.wake_train import TrainConfig

    cfg = TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
    )
    assert cfg.num_positives == 600
    assert cfg.num_voices == 4
    assert cfg.quick is False
    assert cfg.keep_cache is False


def test_train_config_quick_overrides_num_positives_via_helper(tmp_path):
    from opencomputer.voice.wake_train import TrainConfig, effective_positives

    cfg = TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
        quick=True,
    )
    assert effective_positives(cfg) == 50  # quick mode shortcut


def test_train_config_validates_word(tmp_path):
    from opencomputer.voice.wake_train import TrainConfig, WakeTrainError

    with pytest.raises(WakeTrainError, match="word"):
        TrainConfig(
            word="",
            out_path=tmp_path / "out.onnx",
            profile_home=tmp_path,
        )
    with pytest.raises(WakeTrainError, match="word"):
        TrainConfig(
            word="bad word with spaces",
            out_path=tmp_path / "out.onnx",
            profile_home=tmp_path,
        )


def test_ensure_deps_raises_on_missing(monkeypatch):
    from opencomputer.voice.wake_train import WakeTrainError, ensure_deps

    # Force ImportError for openwakeword.train.
    monkeypatch.setitem(sys.modules, "openwakeword.train", None)
    with pytest.raises(WakeTrainError, match=r"opencomputer\[wake-train\]"):
        ensure_deps()


# ---------------------------------------------------------------------------
# Phase 3.2 — synthesize_positives
# ---------------------------------------------------------------------------


def test_synthesize_positives_round_robins_voices(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    captured_voices: list[str] = []

    def fake_synth(*, text, out_path, voice, length_scale, noise_scale,
                   noise_w_scale):
        captured_voices.append(voice)
        out_path.write_bytes(b"WAVE-fake")

    monkeypatch.setattr(wake_train, "_synthesize_one", fake_synth)
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
        num_positives=12,
        num_voices=3,
    )
    out_dir = tmp_path / "positives"
    paths = wake_train.synthesize_positives(
        cfg, out_dir=out_dir, progress=lambda _msg: None, rng_seed=42,
    )
    assert len(paths) == 12
    # 12 positives across 3 voices => exactly 4 per voice (round-robin).
    voice_counts = {v: captured_voices.count(v) for v in set(captured_voices)}
    assert all(c == 4 for c in voice_counts.values())
    assert len(voice_counts) == 3


def test_synthesize_positives_jitters_prosody(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    seen: list[tuple[float, float, float]] = []

    def fake_synth(*, text, out_path, voice, length_scale, noise_scale,
                   noise_w_scale):
        seen.append((length_scale, noise_scale, noise_w_scale))
        out_path.write_bytes(b"WAVE-fake")

    monkeypatch.setattr(wake_train, "_synthesize_one", fake_synth)
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
        num_positives=20,
        num_voices=2,
    )
    wake_train.synthesize_positives(
        cfg, out_dir=tmp_path / "positives", progress=lambda _: None,
        rng_seed=42,
    )
    # All values within their declared ranges + at least 5 distinct triplets
    # (deterministic seed gives us reproducible variance).
    for ls, ns, nws in seen:
        assert 0.85 <= ls <= 1.15
        assert 0.55 <= ns <= 0.75
        assert 0.65 <= nws <= 0.95
    assert len({tuple(t) for t in seen}) >= 5


def test_synthesize_positives_renders_word_with_spaces(tmp_path, monkeypatch):
    """Underscores in the word are converted to spaces for the TTS phrase."""
    from opencomputer.voice import wake_train

    seen_text: list[str] = []

    def fake_synth(*, text, out_path, voice, length_scale, noise_scale,
                   noise_w_scale):
        seen_text.append(text)
        out_path.write_bytes(b"WAVE-fake")

    monkeypatch.setattr(wake_train, "_synthesize_one", fake_synth)
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
        num_positives=12,
        num_voices=1,
    )
    wake_train.synthesize_positives(
        cfg, out_dir=tmp_path / "positives", progress=lambda _: None,
        rng_seed=42,
    )
    assert all(t == "hey open computer" for t in seen_text)
    assert len(seen_text) == 12


# ---------------------------------------------------------------------------
# Phase 3.3 — ensure_negatives
# ---------------------------------------------------------------------------


def test_ensure_negatives_uses_cache_when_present(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    # Pre-populate cache.
    cache = tmp_path / "cache" / "wake_train" / "_negatives"
    cache.mkdir(parents=True)
    (cache / "neg_00001.wav").write_bytes(b"WAV")
    (cache / "neg_00002.wav").write_bytes(b"WAV")

    fetched: list[bool] = []

    def fake_snapshot_download(*, repo_id, repo_type, local_dir, **kwargs):
        fetched.append(True)
        return str(local_dir)

    monkeypatch.setattr(
        wake_train, "_snapshot_download", fake_snapshot_download,
    )
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
    )
    result = wake_train.ensure_negatives(cfg, progress=lambda _: None)
    assert result == cache
    assert fetched == []  # cache was warm; no fetch


def test_ensure_negatives_downloads_when_cold(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    fetched_repo: list[str] = []

    def fake_snapshot_download(*, repo_id, repo_type, local_dir, **kwargs):
        fetched_repo.append(repo_id)
        # Simulate the download dropping a few files.
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (Path(local_dir) / f"neg_{i:05d}.wav").write_bytes(b"WAV")
        return str(local_dir)

    monkeypatch.setattr(
        wake_train, "_snapshot_download", fake_snapshot_download,
    )
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
    )
    result = wake_train.ensure_negatives(cfg, progress=lambda _: None)
    assert result.exists()
    assert fetched_repo == [wake_train._NEGATIVES_HF_REPO]
    assert len(list(result.glob("*.wav"))) == 3


# ---------------------------------------------------------------------------
# Phase 3.4 — config YAML + subprocess invocation
# ---------------------------------------------------------------------------


def test_write_training_config_yaml_shape(tmp_path):
    from opencomputer.voice import wake_train

    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
        num_positives=600,
    )
    yaml_path = wake_train.write_training_config(
        cfg,
        cache_dir=tmp_path / "cache",
        positives_dir=tmp_path / "cache" / "positives",
        negatives_dir=tmp_path / "cache" / "_negatives",
    )
    assert yaml_path.is_file()
    text = yaml_path.read_text()
    assert "hey open computer" in text
    assert "model_name" in text
    assert "epochs" in text
    assert str(tmp_path / "cache" / "positives") in text


def test_invoke_openwakeword_train_streams_progress(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    captured: list[str] = []

    class FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self._lines = ["epoch 1/2\n", "epoch 2/2\n", "training complete\n"]
            self.stdout = iter(self._lines)

        def wait(self) -> int:
            return 0

        def send_signal(self, sig) -> None:
            pass

    fake_proc = FakeProcess()

    # Pre-create the model file the trainer is supposed to produce.
    cache = tmp_path / "cache"
    model_out = cache / "model_output"
    model_out.mkdir(parents=True)
    (model_out / "hey_open_computer.onnx").write_bytes(b"ONNX-fake")

    def fake_popen(cmd, **kwargs):
        return fake_proc

    monkeypatch.setattr(wake_train.subprocess, "Popen", fake_popen)

    yaml_path = cache / "config.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text("dummy: 1\n")

    onnx = wake_train.invoke_openwakeword_train(
        config_yaml=yaml_path,
        cache_dir=cache,
        word="hey_open_computer",
        progress=lambda msg: captured.append(msg),
    )
    assert onnx == model_out / "hey_open_computer.onnx"
    assert any("epoch 1/2" in m for m in captured)


def test_invoke_openwakeword_train_raises_on_nonzero(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    class FakeProcess:
        returncode = 7

        def __init__(self) -> None:
            self.stdout = iter(["boom\n"])

        def wait(self) -> int:
            return 7

        def send_signal(self, sig) -> None:
            pass

    monkeypatch.setattr(
        wake_train.subprocess, "Popen", lambda cmd, **kw: FakeProcess(),
    )
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    yaml_path = cache / "config.yaml"
    yaml_path.write_text("x: 1\n")
    with pytest.raises(wake_train.WakeTrainError, match="exit 7"):
        wake_train.invoke_openwakeword_train(
            config_yaml=yaml_path,
            cache_dir=cache,
            word="hey_open_computer",
            progress=lambda _: None,
        )


# ---------------------------------------------------------------------------
# Phase 3.5 — sanity check + atomic rename + run_training
# ---------------------------------------------------------------------------


def test_atomic_rename_creates_parents(tmp_path):
    from opencomputer.voice import wake_train

    src = tmp_path / "src.onnx"
    src.write_bytes(b"ONNX")
    dst = tmp_path / "deep" / "nested" / "out.onnx"
    wake_train.atomic_rename(src, dst)
    assert dst.is_file()
    assert not src.exists()


def test_sanity_check_onnx_returns_false_on_corrupt(tmp_path):
    from opencomputer.voice import wake_train

    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not really an onnx")
    assert wake_train.sanity_check_onnx(bad) is False


def test_run_training_quick_path_orchestrates_phases(tmp_path, monkeypatch):
    """Quick run with all heavy phases mocked: assert end-to-end flow."""
    from opencomputer.voice import wake_train

    progress_msgs: list[str] = []

    monkeypatch.setattr(wake_train, "ensure_deps", lambda: None)

    def fake_synth(cfg, *, out_dir, progress, rng_seed=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(wake_train.effective_positives(cfg)):
            p = out_dir / f"pos_{i}.wav"
            p.write_bytes(b"WAV")
            paths.append(p)
        progress("synth done")
        return paths

    def fake_neg(cfg, *, progress):
        d = wake_train._negatives_cache_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        (d / "neg_0.wav").write_bytes(b"WAV")
        progress("neg done")
        return d

    def fake_invoke(*, config_yaml, cache_dir, word, progress):
        out = cache_dir / "model_output" / f"{word}.onnx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ONNX-fake")
        progress("trained")
        return out

    monkeypatch.setattr(wake_train, "synthesize_positives", fake_synth)
    monkeypatch.setattr(wake_train, "ensure_negatives", fake_neg)
    monkeypatch.setattr(wake_train, "invoke_openwakeword_train", fake_invoke)
    monkeypatch.setattr(wake_train, "sanity_check_onnx", lambda p: True)

    out = tmp_path / "wake_models" / "hey_open_computer.onnx"
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=out,
        profile_home=tmp_path,
        quick=True,
    )
    result = wake_train.run_training(cfg, progress=progress_msgs.append)
    assert result.out_path == out
    assert out.is_file()
    assert result.sanity_ok is True
    assert result.num_positives == 50  # quick
    # cache cleaned on success unless keep_cache.
    assert not result.cache_dir.exists()
    assert any("synth" in m for m in progress_msgs)
    assert any("trained" in m for m in progress_msgs)


def test_run_training_keeps_cache_on_keep_cache(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    monkeypatch.setattr(wake_train, "ensure_deps", lambda: None)

    def fake_synth(cfg, *, out_dir, progress, rng_seed=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        return []

    def fake_neg(cfg, *, progress):
        d = wake_train._negatives_cache_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fake_invoke(*, config_yaml, cache_dir, word, progress):
        out = cache_dir / "model_output" / f"{word}.onnx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ONNX")
        return out

    monkeypatch.setattr(wake_train, "synthesize_positives", fake_synth)
    monkeypatch.setattr(wake_train, "ensure_negatives", fake_neg)
    monkeypatch.setattr(wake_train, "invoke_openwakeword_train", fake_invoke)
    monkeypatch.setattr(wake_train, "sanity_check_onnx", lambda p: True)

    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out" / "hey_open_computer.onnx",
        profile_home=tmp_path,
        quick=True,
        keep_cache=True,
    )
    result = wake_train.run_training(cfg, progress=lambda _: None)
    assert result.cache_dir.exists()


def test_run_training_preserves_cache_on_failure(tmp_path, monkeypatch):
    from opencomputer.voice import wake_train

    monkeypatch.setattr(wake_train, "ensure_deps", lambda: None)

    def boom_synth(cfg, *, out_dir, progress, rng_seed=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        raise wake_train.WakeTrainError("synth failed", phase="synthesize")

    monkeypatch.setattr(wake_train, "synthesize_positives", boom_synth)

    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out" / "hey_open_computer.onnx",
        profile_home=tmp_path,
        quick=True,
    )
    with pytest.raises(wake_train.WakeTrainError, match="synth failed"):
        wake_train.run_training(cfg, progress=lambda _: None)
    # Failure should preserve the run's tempdir.
    runs = list(
        (tmp_path / "cache" / "wake_train").glob("hey_open_computer-*"),
    )
    assert runs, "failure should preserve the run's tempdir"


# ---------------------------------------------------------------------------
# Phase 4 — CLI
# ---------------------------------------------------------------------------


def test_cli_train_wake_missing_deps_exit_3(monkeypatch):
    """Missing deps → CLI exits with code 3."""
    from typer.testing import CliRunner

    from opencomputer.cli_voice import voice_app
    from opencomputer.voice import wake_train

    def boom_ensure():
        raise wake_train.WakeTrainError(
            "wake-train deps missing: torch — install with "
            "`pip install opencomputer[wake-train]`",
            phase="ensure_deps",
        )

    monkeypatch.setattr(wake_train, "ensure_deps", boom_ensure)
    runner = CliRunner()
    result = runner.invoke(voice_app, ["train-wake"])
    assert result.exit_code == 3
    assert "wake-train" in result.output


def test_cli_train_wake_runs_quick_pipeline(tmp_path, monkeypatch):
    """End-to-end: --quick run completes; ONNX written at expected path."""
    from typer.testing import CliRunner

    from opencomputer.cli_voice import voice_app
    from opencomputer.voice import wake_train

    monkeypatch.setattr(wake_train, "ensure_deps", lambda: None)

    captured_cfgs: list[wake_train.TrainConfig] = []

    def fake_run_training(cfg, *, progress=None):
        captured_cfgs.append(cfg)
        cfg.out_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.out_path.write_bytes(b"ONNX-fake")
        return wake_train.TrainResult(
            out_path=cfg.out_path,
            duration_seconds=2.0,
            num_positives=50,
            num_negatives=10,
            sanity_ok=True,
            cache_dir=tmp_path / "cache_dummy",
        )

    monkeypatch.setattr(wake_train, "run_training", fake_run_training)
    monkeypatch.setattr(
        "opencomputer.cli_voice._resolve_profile_home", lambda: tmp_path,
    )

    runner = CliRunner()
    out_path = tmp_path / "wake_models" / "hey_open_computer.onnx"
    result = runner.invoke(voice_app, ["train-wake", "--quick"])
    assert result.exit_code == 0, result.output
    assert out_path.is_file()
    assert "hey_open_computer.onnx" in result.output
    # CLI threaded --quick through to the config.
    assert len(captured_cfgs) == 1
    assert captured_cfgs[0].quick is True
