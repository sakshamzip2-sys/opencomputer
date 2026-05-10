"""Custom wake-word training pipeline (on-demand, CPU-only, ~30 min).

End-to-end: piper-tts positives → HuggingFace negatives →
``python -m openwakeword.train`` subprocess → ONNX sanity check →
atomic-rename to ``<profile_home>/wake_models/<word>.onnx``.

Triggered ONLY by the ``oc voice train-wake`` CLI command. No background
work, no auto-train. Heavy deps (torch, openwakeword[train],
huggingface_hub) live behind the ``[wake-train]`` extra.

Spec: docs/superpowers/specs/2026-05-07-wake-word-custom-training-design.md

Design notes:
    * Subprocess-out to ``openwakeword.train`` rather than importing
      ``openwakeword.train.Trainer`` — the CLI is the documented contract;
      internals shifted between 0.5 and 0.6.
    * Cross-platform sample synthesis via the existing ``tts_piper.py``
      helper, sidestepping the Linux-only ``piper-sample-generator``.
    * Fresh tempdir per run + atomic rename on success — never corrupts
      a previously-trained model on a crashed run.
"""

from __future__ import annotations

import importlib
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("opencomputer.voice.wake_train")

#: Quick mode: smoke run that exercises the full pipeline in ~2 min.
#: The resulting ONNX is NOT usable for real wake detection — it's a
#: pipeline plumbing check. Use the real run (default 600 samples) for
#: production.
_QUICK_POSITIVES: int = 50

#: HuggingFace dataset slug for negative audio (background / non-target
#: speech and noise). Cached after first download.
_NEGATIVES_HF_REPO: str = "dscripka/audioset_500m_train_clips"

#: Sample rate openwakeword expects for both positives and negatives.
_SAMPLE_RATE: int = 16_000

#: 4 default Piper voices: 2 US, 2 UK; mix of male/female. Each voice
#: contributes ~1/N of the positive sample budget. Add to taste with
#: ``TrainConfig.voices``.
_DEFAULT_VOICES: tuple[str, ...] = (
    "en_US-lessac-medium",
    "en_US-amy-medium",
    "en_GB-jenny_dioco-medium",
    "en_GB-ryan-medium",
)

#: Per-sample prosody jitter — uniform random in these closed intervals.
_LENGTH_SCALE_RANGE: tuple[float, float] = (0.85, 1.15)
_NOISE_SCALE_RANGE: tuple[float, float] = (0.55, 0.75)
_NOISE_W_SCALE_RANGE: tuple[float, float] = (0.65, 0.95)

#: Lowercased ASCII word/phrase regex — matches ``hey_open_computer``
#: and ``hey_jarvis``; rejects spaces and Unicode. Constrained so the
#: phrase can be safely interpolated into a YAML scalar + filesystem
#: path without quoting / escaping concerns.
_WORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


class WakeTrainError(RuntimeError):
    """Any failure across the training pipeline phases.

    Attributes:
        phase: phase name where the failure occurred (e.g. "synthesize",
            "negatives", "train", "sanity"). Used by the CLI to map
            phase → exit code.
    """

    def __init__(self, message: str, *, phase: str = "unknown") -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(frozen=True, slots=True)
class TrainConfig:
    """Inputs to one training run.

    Args:
        word: lowercase phrase, underscores instead of spaces. The
            user-visible label of the wake-word.
        out_path: where to write the final ONNX. Atomic rename applies.
        profile_home: profile root used for cache + tempdir locations.
        num_positives: synthesized utterance budget. Bigger ≈ better
            recall, longer training. Default 600 (~30 min CPU); 1500
            ≈ 60 min CPU, generally improves recall by ~5-10 ppts.
        num_voices: how many of ``_DEFAULT_VOICES`` to round-robin.
            Capped at len(_DEFAULT_VOICES); 4 = all.
        voices: explicit voice list override. Empty tuple → use
            ``_DEFAULT_VOICES[:num_voices]``.
        quick: smoke mode (50 positives, 2 epochs). Output not usable.
        keep_cache: leave the per-run tempdir on success (debugging).
    """

    word: str
    out_path: Path
    profile_home: Path
    num_positives: int = 600
    num_voices: int = 4
    voices: tuple[str, ...] = ()
    quick: bool = False
    keep_cache: bool = False

    def __post_init__(self) -> None:
        if not _WORD_RE.fullmatch(self.word):
            raise WakeTrainError(
                f"invalid word {self.word!r}: must match "
                f"{_WORD_RE.pattern} (lowercase, underscores, no spaces)",
                phase="config",
            )
        if self.num_positives < 10:
            raise WakeTrainError(
                f"num_positives={self.num_positives} too small (min 10)",
                phase="config",
            )


@dataclass(frozen=True, slots=True)
class TrainResult:
    """Outcome of a successful training run.

    Args:
        out_path: written ONNX (post atomic-rename).
        duration_seconds: wall-clock total.
        num_positives: actual count of positives synthesized.
        num_negatives: count of negative clips referenced.
        sanity_ok: True iff openwakeword.Model loaded the ONNX and
            ran a 80 ms silence frame through it without crashing.
        cache_dir: per-run tempdir (preserved on failure or with
            ``keep_cache=True``).
    """

    out_path: Path
    duration_seconds: float
    num_positives: int
    num_negatives: int
    sanity_ok: bool
    cache_dir: Path


def effective_positives(cfg: TrainConfig) -> int:
    """Return the actual positive-sample count given ``cfg.quick``."""
    return _QUICK_POSITIVES if cfg.quick else cfg.num_positives


def ensure_deps() -> None:
    """Verify the ``[wake-train]`` extra is installed; raise otherwise.

    Raises:
        WakeTrainError: at least one training dep is missing. The
            error message points at ``pip install opencomputer[wake-train]``.
    """
    required = (
        "openwakeword.train",
        "torch",
        "huggingface_hub",
        "soundfile",
        "piper",
    )
    missing: list[str] = []
    for module_name in required:
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        raise WakeTrainError(
            "wake-train deps missing: "
            + ", ".join(missing)
            + " — install with `pip install opencomputer[wake-train]`",
            phase="ensure_deps",
        )


def wake_models_dir_for_profile(profile_home: Path) -> Path:
    """Return ``<profile_home>/wake_models/`` (no mkdir — caller's job)."""
    return profile_home / "wake_models"


def _resolve_voice_list(cfg: TrainConfig) -> list[str]:
    """Return the list of Piper voice names this run will use."""
    if cfg.voices:
        return list(cfg.voices)
    cap = min(cfg.num_voices, len(_DEFAULT_VOICES))
    if cap < 1:
        raise WakeTrainError(
            "num_voices must be >= 1", phase="config",
        )
    return list(_DEFAULT_VOICES[:cap])


def _synthesize_one(
    *,
    text: str,
    out_path: Path,
    voice: str,
    length_scale: float,
    noise_scale: float,
    noise_w_scale: float,
) -> None:
    """Synthesize one WAV via piper-tts at 16 kHz mono.

    Thin wrapper to keep ``synthesize_positives`` testable without
    booting Piper. The real implementation calls into the existing
    ``opencomputer.voice.tts_piper.synthesize_to_path`` helper rather
    than duplicating voice-cache logic.
    """
    from opencomputer.voice.tts_piper import (  # noqa: PLC0415
        PiperConfig,
        synthesize_to_path,
    )
    piper_cfg = PiperConfig(
        voice=voice,
        length_scale=length_scale,
        noise_scale=noise_scale,
        noise_w_scale=noise_w_scale,
    )
    synthesize_to_path(text=text, dest=out_path, cfg=piper_cfg)


def synthesize_positives(
    cfg: TrainConfig,
    *,
    out_dir: Path,
    progress: Callable[[str], None],
    rng_seed: int | None = None,
) -> list[Path]:
    """Synthesize ``effective_positives(cfg)`` WAV files of the wake phrase.

    Voices are round-robin'd from ``_resolve_voice_list``; per-call
    prosody is jittered uniformly within ``_LENGTH_SCALE_RANGE``,
    ``_NOISE_SCALE_RANGE``, ``_NOISE_W_SCALE_RANGE``.

    Args:
        cfg: training config.
        out_dir: destination directory; created if missing.
        progress: per-sample progress callback ``progress(msg)``.
        rng_seed: optional deterministic seed (tests).

    Returns:
        List of paths to the written WAVs (length = effective_positives).
    """
    import random  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    voices = _resolve_voice_list(cfg)
    text = cfg.word.replace("_", " ")
    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    target = effective_positives(cfg)
    paths: list[Path] = []
    for i in range(target):
        voice = voices[i % len(voices)]
        ls = rng.uniform(*_LENGTH_SCALE_RANGE)
        ns = rng.uniform(*_NOISE_SCALE_RANGE)
        nws = rng.uniform(*_NOISE_W_SCALE_RANGE)
        out_path = out_dir / f"pos_{i:05d}.wav"
        try:
            _synthesize_one(
                text=text,
                out_path=out_path,
                voice=voice,
                length_scale=ls,
                noise_scale=ns,
                noise_w_scale=nws,
            )
        except Exception as exc:  # noqa: BLE001
            raise WakeTrainError(
                f"piper synthesis failed at sample {i}/{target}: {exc}",
                phase="synthesize",
            ) from exc
        paths.append(out_path)
        if (i + 1) % 25 == 0 or (i + 1) == target:
            progress(f"synthesized {i + 1}/{target} positives")
    return paths


def _negatives_cache_dir(cfg: TrainConfig) -> Path:
    """``<profile_home>/cache/wake_train/_negatives/``."""
    return cfg.profile_home / "cache" / "wake_train" / "_negatives"


def _snapshot_download(
    *, repo_id: str, repo_type: str, local_dir: str, **kwargs,
) -> str:
    """Indirection around ``huggingface_hub.snapshot_download`` for tests.

    Imports lazily so this module remains importable when the
    ``[wake-train]`` extra isn't installed (lets ``ensure_deps`` produce
    a friendlier error than a bare ImportError).
    """
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    return snapshot_download(
        repo_id=repo_id,
        repo_type=repo_type,
        local_dir=local_dir,
        **kwargs,
    )


def ensure_negatives(
    cfg: TrainConfig,
    *,
    progress: Callable[[str], None],
) -> Path:
    """Ensure a cache of negative audio exists; return its directory.

    First call downloads a curated slice of ``_NEGATIVES_HF_REPO`` into
    ``<profile_home>/cache/wake_train/_negatives/``. Subsequent calls
    just return the directory.

    Raises:
        WakeTrainError: download failed (network, auth, or quota).
    """
    cache = _negatives_cache_dir(cfg)
    if cache.is_dir() and any(cache.glob("*.wav")):
        progress(f"negatives cache hit at {cache}")
        return cache
    cache.mkdir(parents=True, exist_ok=True)
    progress(f"downloading negatives ({_NEGATIVES_HF_REPO}) → {cache}")
    try:
        _snapshot_download(
            repo_id=_NEGATIVES_HF_REPO,
            repo_type="dataset",
            local_dir=str(cache),
            allow_patterns=["*.wav"],
        )
    except Exception as exc:  # noqa: BLE001
        raise WakeTrainError(
            f"negatives download failed ({_NEGATIVES_HF_REPO}): {exc}",
            phase="negatives",
        ) from exc
    if not any(cache.glob("*.wav")):
        raise WakeTrainError(
            f"negatives download produced no .wav files in {cache}",
            phase="negatives",
        )
    progress(f"negatives ready at {cache}")
    return cache


def _epochs_for(cfg: TrainConfig) -> int:
    """Choose the epoch count based on quick / num_positives.

    Quick: 2 epochs. Real: 30 epochs (matches openwakeword recommended
    default for the small classifier head). Bigger sample sets
    inherently train longer wall-clock without needing more epochs.
    """
    return 2 if cfg.quick else 30


def write_training_config(
    cfg: TrainConfig,
    *,
    cache_dir: Path,
    positives_dir: Path,
    negatives_dir: Path,
) -> Path:
    """Write the openwakeword-format training YAML.

    Returns the YAML path inside ``cache_dir``. Schema mirrors what
    ``openwakeword/train.py`` expects per the official notebook
    (``automatic_model_training.ipynb``).
    """
    target_phrase = cfg.word.replace("_", " ")
    output_dir = cache_dir / "model_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    n_pos = effective_positives(cfg)
    n_val = max(20, n_pos // 6)
    yaml_text = (
        f"target_phrase:\n"
        f"  - \"{target_phrase}\"\n"
        f"model_name: \"{cfg.word}\"\n"
        f"n_samples: {n_pos}\n"
        f"n_samples_val: {n_val}\n"
        f"batch_size: 64\n"
        f"epochs: {_epochs_for(cfg)}\n"
        f"learning_rate: 0.0001\n"
        f"target_accuracy: 0.7\n"
        f"target_recall: 0.5\n"
        f"target_false_positives_per_hour: 0.5\n"
        f"positive_clips_path: \"{positives_dir}\"\n"
        f"negative_clips_path: \"{negatives_dir}\"\n"
        f"output_dir: \"{output_dir}\"\n"
    )
    yaml_path = cache_dir / "training_config.yaml"
    yaml_path.write_text(yaml_text)
    return yaml_path


def invoke_openwakeword_train(
    *,
    config_yaml: Path,
    cache_dir: Path,
    word: str,
    progress: Callable[[str], None],
) -> Path:
    """Run ``python -m openwakeword.train`` and stream stdout.

    Returns the path to the trained ONNX (under ``cache_dir/model_output/``).
    SIGINT propagates to the child via ``send_signal`` when the calling
    Python process is interrupted.

    Raises:
        WakeTrainError: subprocess exit code != 0, or the expected ONNX
            isn't where the trainer was supposed to write it.
    """
    cmd = [
        sys.executable, "-m", "openwakeword.train",
        "--train_model",
        "--config_path", str(config_yaml),
    ]
    progress(f"launching: {' '.join(cmd)}")
    proc = subprocess.Popen(  # noqa: S603 — sys.executable, fixed args
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                progress(line.rstrip())
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(2)  # SIGINT to the child
        proc.wait()
        raise
    if rc != 0:
        raise WakeTrainError(
            f"openwakeword.train exit {rc} — see {cache_dir} for full logs",
            phase="train",
        )
    expected = cache_dir / "model_output" / f"{word}.onnx"
    if not expected.is_file():
        raise WakeTrainError(
            f"trainer succeeded but ONNX missing at {expected}",
            phase="train",
        )
    return expected


def atomic_rename(src: Path, dst: Path) -> None:
    """``src`` → ``dst`` via tmp-name + rename, creating parent dirs.

    Both paths must be on the same filesystem (the caller is responsible
    for ensuring this — the wake-train pipeline always lands tmp + final
    inside the profile home).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(src, tmp)
    tmp.replace(dst)
    src.unlink(missing_ok=True)


def sanity_check_onnx(onnx_path: Path) -> bool:
    """Load ``onnx_path`` via ``openwakeword.Model`` + 80 ms silence predict.

    Returns True iff the model loads and one ``predict()`` call returns a
    dict-like result. Catches corruption + opset mismatch + missing
    metadata. Failures are logged but never raised — callers gate on
    the return value and decide what to do.
    """
    try:
        import numpy as np  # noqa: PLC0415
        from openwakeword.model import Model  # noqa: PLC0415
    except ImportError as exc:
        _log.warning(
            "sanity_check: openwakeword/numpy not importable: %s", exc,
        )
        return False
    try:
        model = Model(wakeword_models=[str(onnx_path)])
    except Exception as exc:  # noqa: BLE001
        _log.warning("sanity_check: Model() failed: %s", exc)
        return False
    silence = np.zeros(1280, dtype=np.int16)
    try:
        result = model.predict(silence)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sanity_check: predict() failed: %s", exc)
        return False
    return isinstance(result, dict)


def _make_run_cache_dir(cfg: TrainConfig) -> Path:
    """Per-run tempdir under ``<profile_home>/cache/wake_train/<word>-<ts>/``."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = cfg.profile_home / "cache" / "wake_train" / f"{cfg.word}-{ts}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_training(
    cfg: TrainConfig,
    *,
    progress: Callable[[str], None] | None = None,
) -> TrainResult:
    """End-to-end training pipeline.

    Phases:
        1. ensure_deps
        2. synthesize_positives        (~3 min for 600 samples)
        3. ensure_negatives            (~1 min cold; cached after)
        4. write_training_config
        5. invoke_openwakeword_train   (~25 min; the real budget)
        6. sanity_check_onnx
        7. atomic_rename  →  cfg.out_path

    The cache dir is preserved on any phase failure (so the user can
    inspect intermediate state). On success it's removed unless
    ``cfg.keep_cache``.

    Returns a populated :class:`TrainResult`. Raises
    :class:`WakeTrainError` (phase-tagged) on any failure.
    """
    _progress = progress or (lambda _msg: None)
    started = time.monotonic()
    ensure_deps()
    cache_dir = _make_run_cache_dir(cfg)
    success = False
    try:
        positives_dir = cache_dir / "positives"
        positive_paths = synthesize_positives(
            cfg, out_dir=positives_dir, progress=_progress,
        )
        negatives_dir = ensure_negatives(cfg, progress=_progress)
        config_yaml = write_training_config(
            cfg,
            cache_dir=cache_dir,
            positives_dir=positives_dir,
            negatives_dir=negatives_dir,
        )
        trained_onnx = invoke_openwakeword_train(
            config_yaml=config_yaml,
            cache_dir=cache_dir,
            word=cfg.word,
            progress=_progress,
        )
        sanity_ok = sanity_check_onnx(trained_onnx)
        if not sanity_ok:
            raise WakeTrainError(
                f"trained ONNX at {trained_onnx} failed sanity check "
                f"(openwakeword.Model load or predict crashed)",
                phase="sanity",
            )
        atomic_rename(trained_onnx, cfg.out_path)
        success = True
        return TrainResult(
            out_path=cfg.out_path,
            duration_seconds=time.monotonic() - started,
            num_positives=len(positive_paths),
            num_negatives=sum(1 for _ in negatives_dir.glob("*.wav")),
            sanity_ok=True,
            cache_dir=cache_dir,
        )
    finally:
        if success and not cfg.keep_cache:
            shutil.rmtree(cache_dir, ignore_errors=True)


__all__ = [
    "TrainConfig",
    "TrainResult",
    "WakeTrainError",
    "atomic_rename",
    "effective_positives",
    "ensure_deps",
    "ensure_negatives",
    "invoke_openwakeword_train",
    "run_training",
    "sanity_check_onnx",
    "synthesize_positives",
    "wake_models_dir_for_profile",
    "write_training_config",
]
