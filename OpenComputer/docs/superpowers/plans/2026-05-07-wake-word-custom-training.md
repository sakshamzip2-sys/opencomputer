# Custom Wake-Word Training — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `oc voice train-wake` so a user can produce a custom `hey_open_computer.onnx` model on their CPU in ~30 minutes; auto-discover the trained ONNX from the existing detector so `oc voice wake` works without `--model` afterwards.

**Architecture:** A new `opencomputer/voice/wake_train.py` module drives an end-to-end pipeline (synthesize positives via piper-tts → cache HuggingFace negatives → write openwakeword YAML → invoke `python -m openwakeword.train` via subprocess → sanity-check ONNX → atomic-rename into `<profile_home>/wake_models/<word>.onnx`). Existing `WakeWordDetector._resolve_word` gets a 5-LOC patch to auto-discover that path before falling back to `hey_jarvis`. Heavy deps (torch, openwakeword[train], huggingface_hub) live behind a new `[wake-train]` extra.

**Tech Stack:** Python 3.12+, openwakeword>=0.6 (CLI subprocess), piper-tts (existing in voice/tts_piper.py), torch (transitively, training-time only), huggingface_hub, soundfile, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-05-07-wake-word-custom-training-design.md`

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Create | `OpenComputer/opencomputer/voice/wake_train.py` | Training orchestration: TrainConfig, TrainResult, run_training, all phase functions, error type |
| Modify | `OpenComputer/opencomputer/voice/wake_word.py` | Add `_auto_discover_model()`; patch `_resolve_word()` to consult it before fallback; new `wake_models_dir()` helper |
| Modify | `OpenComputer/opencomputer/cli_voice.py` | New `voice train-wake` Typer command |
| Modify | `OpenComputer/opencomputer/doctor.py` | New `_check_wake_train_capable` + registry wiring |
| Modify | `OpenComputer/pyproject.toml` | New `[wake-train]` optional-dependencies entry |
| Create | `OpenComputer/tests/voice/test_wake_train.py` | ~16 unit + 2 integration + 2 CLI + 2 doctor tests |
| Modify | `OpenComputer/tests/voice/test_wake_word.py` | 2 new tests for the auto-discovery patch |
| Modify | `OpenComputer/CHANGELOG.md` | New entry under `[Unreleased]` |

---

## Phase 0 — Baseline

### Task 0.1: Verify worktree state and clean baseline

**Files:** none (verification only)

- [ ] **Step 1: Verify in the right worktree**

Run:
```bash
git rev-parse --show-toplevel
git branch --show-current
```
Expected: path inside `.claude/worktrees/pr-a-steer-wake-acp-2026-05-07`, branch `worktree-pr-a-steer-wake-acp-2026-05-07`.

- [ ] **Step 2: Confirm `OpenComputer/` is the project root**

Run:
```bash
ls OpenComputer/pyproject.toml OpenComputer/opencomputer/voice/wake_word.py
```
Expected: both files exist.

- [ ] **Step 3: Establish a baseline pytest collection**

Run (from worktree root):
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_word.py -q 2>&1 | tail -10
```
Expected: tests pass (or skip gracefully when openwakeword is missing). Note count for later sanity check.

- [ ] **Step 4: Establish baseline ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/ tests/voice/ 2>&1 | tail -5
```
Expected: clean (or only pre-existing warnings).

---

## Phase 1 — `[wake-train]` extra and doctor probe

### Task 1.1: Add the `[wake-train]` extra to pyproject.toml

**Files:**
- Modify: `OpenComputer/pyproject.toml`

- [ ] **Step 1: Locate the `[project.optional-dependencies]` block**

Run:
```bash
grep -n '^\[project.optional-dependencies\]' OpenComputer/pyproject.toml
```
Expected: one line near 63.

- [ ] **Step 2: Add the `wake-train` entry directly after `wake`**

Edit `OpenComputer/pyproject.toml`. Find the existing block (around line 69):

```toml
wake = [
  "openwakeword>=0.6.0",
  "onnxruntime>=1.17",
]
```

Insert immediately after the closing `]`:

```toml
# Custom wake-word training (2026-05-07): produces a hey_open_computer
# (or other custom-phrase) ONNX model on the user's CPU in ~30 min.
#   pip install opencomputer[wake-train]
# Heavy: pulls torch (~2GB) + openwakeword[train] + huggingface_hub for
# negative-sample download. Cross-platform; CPU-only. Verify install with
# `oc doctor wake-train`. Run training: `oc voice train-wake`.
wake-train = [
  "openwakeword[train]>=0.6.0,<0.7",
  "torch>=2.1",
  "huggingface_hub>=0.20",
  "soundfile>=0.12",
  "piper-tts>=1.2",
  "openwakeword>=0.6.0",
  "onnxruntime>=1.17",
]
```

- [ ] **Step 3: Verify TOML still parses**

Run:
```bash
python -c "import tomllib; tomllib.loads(open('OpenComputer/pyproject.toml').read())"
```
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/pyproject.toml
git commit -m "feat(wake-train): pyproject [wake-train] optional extra"
```

### Task 1.2: Doctor probe `_check_wake_train_capable`

**Files:**
- Modify: `OpenComputer/opencomputer/doctor.py`
- Test: `OpenComputer/tests/test_doctor_introspection_checks.py` (verify new line lints; we add dedicated tests in 1.3 below)

- [ ] **Step 1: Locate the wake-word check**

Run:
```bash
grep -n '_check_wake_word_capable' OpenComputer/opencomputer/doctor.py
```
Expected: definition near line 781 + registry wire near line 1246.

- [ ] **Step 2: Insert the new check function directly after `_check_wake_word_capable`**

Edit `OpenComputer/opencomputer/doctor.py`. Find the closing of `_check_wake_word_capable` (the function returns a `CheckResult` near line 845). Append after its final `return`:

```python


def _check_wake_train_capable() -> CheckResult:
    """Verify the [wake-train] extra is installable on this platform.

    Imports each training-time dep (torch, openwakeword.train,
    huggingface_hub, soundfile, piper) and reports info-level if any are
    missing — training is opt-in like wake-word itself.
    """
    missing: list[str] = []
    for module_name, hint in (
        ("torch", "pip install torch>=2.1"),
        ("openwakeword.train", "pip install 'openwakeword[train]>=0.6'"),
        ("huggingface_hub", "pip install huggingface_hub>=0.20"),
        ("soundfile", "pip install soundfile>=0.12"),
        ("piper", "pip install piper-tts>=1.2"),
    ):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(f"{module_name} ({hint})")
    if missing:
        return CheckResult(
            status="info",
            message=(
                "wake-train deps missing: "
                + ", ".join(missing)
                + " — opt in via `pip install opencomputer[wake-train]`"
            ),
        )
    return CheckResult(
        status="ok",
        message="wake-train deps available — `oc voice train-wake` is ready",
    )
```

- [ ] **Step 3: Wire the check into the doctor registry**

Find the existing wire site:

```bash
grep -n 'wake-word' OpenComputer/opencomputer/doctor.py
```
Expected: one match near line 1246 calling `_result_to_check("wake-word", _check_wake_word_capable())`.

Insert immediately after that line:

```python
    # Custom wake-word training preflight — opt-in via [wake-train] extra.
    # Returns info-level when the user hasn't installed the heavy deps.
    checks.append(
        _result_to_check("wake-train", _check_wake_train_capable())
    )
```

- [ ] **Step 4: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/doctor.py
```
Expected: clean.

- [ ] **Step 5: Smoke run doctor**

Run:
```bash
cd OpenComputer && python -c "from opencomputer.doctor import _check_wake_train_capable; print(_check_wake_train_capable())"
```
Expected: a `CheckResult(status='info', message='wake-train deps missing: ...')` line (because `[wake-train]` not installed in dev venv).

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/doctor.py
git commit -m "feat(doctor): wake-train capability probe"
```

### Task 1.3: Doctor tests for `_check_wake_train_capable`

**Files:**
- Test: `OpenComputer/tests/voice/test_wake_train.py` (new — first appearance)

- [ ] **Step 1: Create the test file with two doctor tests**

Create `OpenComputer/tests/voice/test_wake_train.py`:

```python
"""Tests for wake-word custom training pipeline.

Spec: docs/superpowers/specs/2026-05-07-wake-word-custom-training-design.md
Plan: docs/superpowers/plans/2026-05-07-wake-word-custom-training.md
"""

from __future__ import annotations

import sys

import pytest


# ---------------------------------------------------------------------------
# Doctor probe (Task 1.3)
# ---------------------------------------------------------------------------


def test_doctor_wake_train_info_when_deps_missing(monkeypatch):
    """When training deps are missing, doctor reports 'info' (not error)."""
    # Force ImportError for each training dep so we get the info path.
    for mod in ("torch", "openwakeword.train", "huggingface_hub",
                "soundfile", "piper"):
        monkeypatch.setitem(sys.modules, mod, None)
    from opencomputer.doctor import _check_wake_train_capable

    result = _check_wake_train_capable()
    assert result.status == "info"
    assert "wake-train deps missing" in result.message


def test_doctor_wake_train_ok_when_all_deps_present(monkeypatch):
    """When all deps importable, doctor reports 'ok'."""
    import types

    for mod in ("torch", "openwakeword.train", "huggingface_hub",
                "soundfile", "piper"):
        monkeypatch.setitem(sys.modules, mod, types.ModuleType(mod))
    from opencomputer.doctor import _check_wake_train_capable

    result = _check_wake_train_capable()
    assert result.status == "ok"
    assert "ready" in result.message
```

- [ ] **Step 2: Run the new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v
```
Expected: 2 passing.

- [ ] **Step 3: Commit**

```bash
git add OpenComputer/tests/voice/test_wake_train.py
git commit -m "test(doctor): wake-train probe — info-when-missing + ok-when-present"
```

---

## Phase 2 — Auto-discovery patch in `wake_word.py`

### Task 2.1: Add `wake_models_dir()` helper + `_auto_discover_model()`

**Files:**
- Modify: `OpenComputer/opencomputer/voice/wake_word.py`
- Test: `OpenComputer/tests/voice/test_wake_word.py`

- [ ] **Step 1: Write failing test for auto-discovery**

Append to `OpenComputer/tests/voice/test_wake_word.py`:

```python


# ---------------------------------------------------------------------------
# Auto-discovery (Task 2.1)
# ---------------------------------------------------------------------------


def test_wake_models_dir_uses_profile_home(tmp_path, monkeypatch):
    """wake_models_dir resolves to <profile_home>/wake_models/."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import wake_models_dir

    result = wake_models_dir()
    assert result == tmp_path / "wake_models"


def test_auto_discover_model_returns_path_when_present(tmp_path, monkeypatch):
    """_auto_discover_model returns the ONNX path when present on disk."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    models_dir = tmp_path / "wake_models"
    models_dir.mkdir()
    onnx = models_dir / "hey_open_computer.onnx"
    onnx.write_bytes(b"fake")
    from opencomputer.voice.wake_word import _auto_discover_model

    found = _auto_discover_model("hey_open_computer")
    assert found == onnx


def test_auto_discover_model_returns_none_when_missing(tmp_path, monkeypatch):
    """_auto_discover_model returns None when no ONNX is at the path."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import _auto_discover_model

    assert _auto_discover_model("hey_open_computer") is None


def test_resolve_word_uses_auto_discovered_model(tmp_path, monkeypatch):
    """When custom word + no model_path + ONNX on disk, _resolve_word uses it."""
    import sys
    from unittest.mock import MagicMock

    fake_ow = MagicMock()
    monkeypatch.setitem(sys.modules, "openwakeword", fake_ow)
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    models_dir = tmp_path / "wake_models"
    models_dir.mkdir()
    (models_dir / "hey_open_computer.onnx").write_bytes(b"fake")

    from opencomputer.voice.wake_word import WakeWordDetector

    det = WakeWordDetector(word="hey_open_computer")
    active = det._resolve_word()
    assert active == "hey_open_computer"
    assert det.fell_back is False
    assert det.model_path == models_dir / "hey_open_computer.onnx"


def test_resolve_word_still_falls_back_when_no_trained_model(
    tmp_path, monkeypatch,
):
    """No trained ONNX => fallback to hey_jarvis still fires."""
    import sys
    from unittest.mock import MagicMock

    fake_ow = MagicMock()
    monkeypatch.setitem(sys.modules, "openwakeword", fake_ow)
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import (
        FALLBACK_BUNDLED_WORD,
        WakeWordDetector,
    )

    det = WakeWordDetector(word="hey_open_computer")
    active = det._resolve_word()
    assert active == FALLBACK_BUNDLED_WORD
    assert det.fell_back is True
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_word.py::test_wake_models_dir_uses_profile_home -v
```
Expected: FAIL — `wake_models_dir` not defined.

- [ ] **Step 3: Implement helpers and patch `_resolve_word`**

Edit `OpenComputer/opencomputer/voice/wake_word.py`. After the existing module constants block (after `_DETECTION_COOLDOWN_S`), add:

```python


def _resolve_profile_home() -> Path:
    """Return the active profile's home directory.

    Mirrors the logic the CLI uses (active profile via
    ``read_active_profile``, falling back to ``~/.opencomputer/default``).
    Pulled out of the CLI so the detector module can use it without
    importing typer.
    """
    try:
        from opencomputer.profiles import (  # noqa: PLC0415
            profile_home_dir,
            read_active_profile,
        )
        active = read_active_profile() or "default"
        return profile_home_dir(active)
    except Exception:  # noqa: BLE001
        return Path.home() / ".opencomputer" / "default"


def wake_models_dir() -> Path:
    """Return ``<profile_home>/wake_models/`` (created on demand by callers)."""
    return _resolve_profile_home() / "wake_models"


def _auto_discover_model(word: str) -> Path | None:
    """Look for ``<profile_home>/wake_models/<word>.onnx``.

    Returns the path when present and non-empty; ``None`` otherwise.
    Used by :meth:`WakeWordDetector._resolve_word` to pick up a model
    that the user trained via ``oc voice train-wake`` without requiring
    them to pass ``--model`` on every wake invocation.
    """
    candidate = wake_models_dir() / f"{word}.onnx"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None
```

Then patch `_resolve_word` (around line 226). Replace the body that handles the fallback case with auto-discovery first:

```python
    def _resolve_word(self) -> str:
        """Pick the actual wake-word to listen for.

        Order:
          1. ``model_path`` set → use ``self.word`` as the label.
          2. ``self.word`` is in ``BUNDLED_WAKE_WORDS`` → use as-is.
          3. Auto-discovered model at ``<profile_home>/wake_models/<word>.onnx``
             → use it (sets ``model_path`` for downstream loaders).
          4. Otherwise → fall back to ``FALLBACK_BUNDLED_WORD``.
        """
        if self.model_path is not None:
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        if self.word in BUNDLED_WAKE_WORDS:
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        # NEW: auto-discover a trained ONNX before fallback.
        auto_path = _auto_discover_model(self.word)
        if auto_path is not None:
            _log.info(
                "wake: auto-discovered trained model at %s", auto_path,
            )
            self.model_path = auto_path
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        # Custom word requested but no model_path → fall back.
        _log.warning(
            "wake: custom wake-word '%s' is not bundled and no model_path "
            "provided; falling back to '%s'. Train a custom model with "
            "`oc voice train-wake` (~30 min on CPU). Reference: %s",
            self.word, FALLBACK_BUNDLED_WORD, TRAINING_URL,
        )
        self._effective_word = FALLBACK_BUNDLED_WORD
        self._fell_back = True
        return FALLBACK_BUNDLED_WORD
```

- [ ] **Step 4: Update the `__all__` export list**

In the same file, find the existing `__all__` near the bottom and update:

```python
__all__ = [
    "BUNDLED_WAKE_WORDS",
    "FALLBACK_BUNDLED_WORD",
    "TRAINING_URL",
    "WAKE_FRAME_SAMPLES",
    "WAKE_SAMPLE_RATE",
    "WakeDetection",
    "WakeState",
    "WakeWordDetector",
    "WakeWordError",
    "_acquire_pid_lock",
    "_auto_discover_model",
    "_resolve_profile_home",
    "wake_models_dir",
]
```

- [ ] **Step 5: Run the new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_word.py -v 2>&1 | tail -25
```
Expected: all pass (existing 14 + 5 new = 19).

- [ ] **Step 6: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/wake_word.py tests/voice/test_wake_word.py
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_word.py OpenComputer/tests/voice/test_wake_word.py
git commit -m "feat(wake): auto-discover trained ONNX from <profile_home>/wake_models/"
```

---

## Phase 3 — `wake_train.py` core module

### Task 3.1: TrainConfig, TrainResult, WakeTrainError, ensure_deps

**Files:**
- Create / Modify: `OpenComputer/opencomputer/voice/wake_train.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing tests for the dataclasses + dep gate**

Add to `OpenComputer/tests/voice/test_wake_train.py`:

```python


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
    with pytest.raises(WakeTrainError, match="opencomputer\\[wake-train\\]"):
        ensure_deps()
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_train_config_defaults -v
```
Expected: FAIL — `wake_train` not importable.

- [ ] **Step 3: Create `wake_train.py` with the dataclasses + dep gate**

Create `OpenComputer/opencomputer/voice/wake_train.py`:

```python
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
from dataclasses import dataclass, field
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
#: and ``hey_jarvis``; rejects spaces and Unicode.
_WORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


class WakeTrainError(RuntimeError):
    """Any failure across the training pipeline phases.

    Attributes:
        phase: phase name where the failure occurred (e.g. "synthesize").
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
            ran a 80ms silence frame through it without crashing.
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


__all__ = [
    "TrainConfig",
    "TrainResult",
    "WakeTrainError",
    "effective_positives",
    "ensure_deps",
    "wake_models_dir_for_profile",
]


def wake_models_dir_for_profile(profile_home: Path) -> Path:
    """Return ``<profile_home>/wake_models/`` (no mkdir — caller's job)."""
    return profile_home / "wake_models"
```

- [ ] **Step 4: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -25
```
Expected: 6 passing (2 doctor from 1.3 + 4 new from 3.1).

- [ ] **Step 5: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/wake_train.py tests/voice/test_wake_train.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_train.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(wake-train): TrainConfig, TrainResult, WakeTrainError, ensure_deps"
```

### Task 3.2: synthesize_positives — piper-tts driver with prosody jitter

**Files:**
- Modify: `OpenComputer/opencomputer/voice/wake_train.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/voice/test_wake_train.py`:

```python


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
    paths = wake_train.synthesize_positives(cfg, out_dir=out_dir,
                                            progress=lambda _msg: None)
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
    wake_train.synthesize_positives(cfg, out_dir=tmp_path / "positives",
                                    progress=lambda _: None)
    # All values within their declared ranges + at least 5 distinct triplets
    # (jitter actually fires).
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
        num_positives=4,
        num_voices=1,
    )
    wake_train.synthesize_positives(cfg, out_dir=tmp_path / "positives",
                                    progress=lambda _: None)
    assert all(t == "hey open computer" for t in seen_text)
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_synthesize_positives_round_robins_voices -v
```
Expected: FAIL — `synthesize_positives` not defined.

- [ ] **Step 3: Implement `synthesize_positives` + `_synthesize_one` thin wrapper**

Add to `opencomputer/voice/wake_train.py` (after `wake_models_dir_for_profile`):

```python


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
    ``opencomputer.voice.tts_piper`` helper rather than duplicating
    voice-cache logic.
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
    import random

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
```

Update `__all__` to include the new public names:

```python
__all__ = [
    "TrainConfig",
    "TrainResult",
    "WakeTrainError",
    "effective_positives",
    "ensure_deps",
    "synthesize_positives",
    "wake_models_dir_for_profile",
]
```

- [ ] **Step 4: Add a tiny `synthesize_to_path` shim to `tts_piper.py` if absent**

Run:
```bash
grep -n 'def synthesize_to_path\|def synthesize_speech' OpenComputer/opencomputer/voice/tts_piper.py | head -5
```

If `synthesize_to_path` is **not** in tts_piper.py, append a thin shim. If it IS present, **skip this step**.

If absent, append at the bottom of `OpenComputer/opencomputer/voice/tts_piper.py`:

```python


def synthesize_to_path(*, text: str, dest: Path, cfg: PiperConfig) -> None:
    """Synthesize ``text`` and write a 16 kHz mono WAV to ``dest``.

    Thin wrapper around the existing module-level helpers used by the
    voice-mode TTS pipeline. Used by wake-train for positive-sample
    generation.
    """
    piper = _import_piper()
    voice_path = _resolve_voice_path(cfg.voice)
    voice = piper.PiperVoice.load(str(voice_path), use_cuda=cfg.use_cuda)
    synth_kwargs: dict[str, float] = {}
    for key, value in (
        ("length_scale", cfg.length_scale),
        ("noise_scale", cfg.noise_scale),
        ("noise_w_scale", cfg.noise_w_scale),
        ("volume", cfg.volume),
    ):
        if value is not None:
            synth_kwargs[key] = value
    if cfg.normalize_audio is not None:
        synth_kwargs["normalize_audio"] = cfg.normalize_audio
    with open(dest, "wb") as fh:
        voice.synthesize_wav(text, fh, **synth_kwargs)


__all__ = list(globals().get("__all__", [])) + ["synthesize_to_path"]
```

- [ ] **Step 5: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -20
```
Expected: 9 passing (6 from prior + 3 new).

- [ ] **Step 6: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/wake_train.py opencomputer/voice/tts_piper.py tests/voice/test_wake_train.py
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_train.py OpenComputer/opencomputer/voice/tts_piper.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(wake-train): synthesize_positives — piper-tts cross-platform driver with prosody jitter"
```

### Task 3.3: ensure_negatives — HuggingFace cache

**Files:**
- Modify: `OpenComputer/opencomputer/voice/wake_train.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/voice/test_wake_train.py`:

```python


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

    monkeypatch.setattr(wake_train, "_snapshot_download",
                        fake_snapshot_download)
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

    monkeypatch.setattr(wake_train, "_snapshot_download",
                        fake_snapshot_download)
    cfg = wake_train.TrainConfig(
        word="hey_open_computer",
        out_path=tmp_path / "out.onnx",
        profile_home=tmp_path,
    )
    result = wake_train.ensure_negatives(cfg, progress=lambda _: None)
    assert result.exists()
    assert fetched_repo == [wake_train._NEGATIVES_HF_REPO]
    assert len(list(result.glob("*.wav"))) == 3
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_ensure_negatives_uses_cache_when_present -v
```
Expected: FAIL — `ensure_negatives` not defined.

- [ ] **Step 3: Implement `ensure_negatives` + helper**

Append to `opencomputer/voice/wake_train.py` (after `synthesize_positives`):

```python


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
```

Update `__all__`:

```python
__all__ = [
    "TrainConfig",
    "TrainResult",
    "WakeTrainError",
    "effective_positives",
    "ensure_deps",
    "ensure_negatives",
    "synthesize_positives",
    "wake_models_dir_for_profile",
]
```

- [ ] **Step 4: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -20
```
Expected: 11 passing (9 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_train.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(wake-train): ensure_negatives — HuggingFace dataset cache"
```

### Task 3.4: write_training_config + invoke_openwakeword_train

**Files:**
- Modify: `OpenComputer/opencomputer/voice/wake_train.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/voice/test_wake_train.py`:

```python


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
            self.stdout_lines = iter([
                "epoch 1/2\n", "epoch 2/2\n", "training complete\n",
            ])

        @property
        def stdout(self):
            return self  # acts as iter() target

        def __iter__(self):
            return self.stdout_lines

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
        stdout_lines = iter(["boom\n"])

        @property
        def stdout(self):
            return self.stdout_lines

        def __iter__(self):
            return self.stdout_lines

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
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_write_training_config_yaml_shape -v
```
Expected: FAIL — `write_training_config` not defined.

- [ ] **Step 3: Implement `write_training_config` + `invoke_openwakeword_train`**

Append to `opencomputer/voice/wake_train.py`:

```python


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
            for line in proc.stdout:  # type: ignore[union-attr]
                progress(line.rstrip())
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(2)  # SIGINT
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
```

Update `__all__`:

```python
__all__ = [
    "TrainConfig",
    "TrainResult",
    "WakeTrainError",
    "effective_positives",
    "ensure_deps",
    "ensure_negatives",
    "invoke_openwakeword_train",
    "synthesize_positives",
    "wake_models_dir_for_profile",
    "write_training_config",
]
```

- [ ] **Step 4: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -25
```
Expected: 14 passing (11 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_train.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(wake-train): YAML config + openwakeword.train subprocess driver"
```

### Task 3.5: sanity_check_onnx + atomic_rename + run_training orchestrator

**Files:**
- Modify: `OpenComputer/opencomputer/voice/wake_train.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/voice/test_wake_train.py`:

```python


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
    # cache cleaned on success unless keep_cache
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
    # cache_dir is per-run; we look for any wake_train run dir in the
    # profile cache. Must still exist after failure.
    runs = list((tmp_path / "cache" / "wake_train").glob("hey_open_computer-*"))
    assert runs, "failure should preserve the run's tempdir"
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_atomic_rename_creates_parents -v
```
Expected: FAIL.

- [ ] **Step 3: Implement the missing functions**

Append to `opencomputer/voice/wake_train.py`:

```python


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
    """Load ``onnx_path`` via ``openwakeword.Model`` + 80ms silence predict.

    Returns True iff the model loads and one predict() call returns a
    dict-like result. Catches corruption + opset mismatch + missing
    metadata. Failures are logged but never raised — callers gate on
    the return value and decide what to do.
    """
    try:
        import numpy as np  # noqa: PLC0415
        from openwakeword.model import Model  # noqa: PLC0415
    except ImportError as exc:
        _log.warning("sanity_check: openwakeword/numpy not importable: %s", exc)
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
        2. synthesize_positives  (~3 min for 600 samples)
        3. ensure_negatives      (~1 min cold; cached after)
        4. write_training_config
        5. invoke_openwakeword_train  (~25 min; the real budget)
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
```

Update `__all__`:

```python
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
```

- [ ] **Step 4: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -25
```
Expected: 19 passing (14 prior + 5 new).

- [ ] **Step 5: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/wake_train.py tests/voice/test_wake_train.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/voice/wake_train.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(wake-train): run_training orchestrator + sanity_check_onnx + atomic_rename"
```

---

## Phase 4 — CLI command `oc voice train-wake`

### Task 4.1: Wire the CLI command

**Files:**
- Modify: `OpenComputer/opencomputer/cli_voice.py`
- Test: `OpenComputer/tests/voice/test_wake_train.py`

- [ ] **Step 1: Append failing CLI tests**

Add to `tests/voice/test_wake_train.py`:

```python


# ---------------------------------------------------------------------------
# Phase 4 — CLI
# ---------------------------------------------------------------------------


def test_cli_train_wake_missing_deps_exit_3(monkeypatch):
    """Missing deps → CLI exits with code 3 (matching wake-word convention)."""
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

    def fake_run_training(cfg, *, progress=None):
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
```

- [ ] **Step 2: Run failing test**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py::test_cli_train_wake_missing_deps_exit_3 -v
```
Expected: FAIL — `train-wake` command not registered.

- [ ] **Step 3: Add the CLI command + profile-home shim to `cli_voice.py`**

Edit `OpenComputer/opencomputer/cli_voice.py`. At the top of the file, after the existing `from pathlib import Path`, add a thin `_resolve_profile_home` shim if not already imported (so tests can monkeypatch a single symbol):

Look for the block in `voice_wake` (around line 522) that resolves `profile_home`. Lift it into a module-level helper. Add this near the top of the file (after the imports):

```python


def _resolve_profile_home() -> Path:
    """Resolve the active profile's home directory (CLI-side).

    Same logic the wake CLI uses; pulled out so the new
    ``train-wake`` command can share it (and tests can monkeypatch it).
    """
    try:
        from opencomputer.profiles import (  # noqa: PLC0415
            profile_home_dir,
            read_active_profile,
        )
        active = read_active_profile() or "default"
        return profile_home_dir(active)
    except Exception:  # noqa: BLE001
        return Path.home() / ".opencomputer" / "default"
```

Then replace the existing inline resolution in `voice_wake` (the block starting `try:\n        from opencomputer.profiles import ...`) with `profile_home = _resolve_profile_home()`.

Now append the new `train-wake` command at the bottom of the file (above `__all__`):

```python


@voice_app.command("train-wake")
def voice_train_wake(
    word: Annotated[
        str,
        typer.Option(
            "--word",
            help="Wake-word phrase. Lowercase, underscores instead of spaces "
                 "(default: hey_open_computer).",
        ),
    ] = "hey_open_computer",
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output ONNX path (default: <profile_home>/wake_models/<word>.onnx).",
        ),
    ] = None,
    samples: Annotated[
        int,
        typer.Option(
            "--samples", min=100, max=5000,
            help="Synthesized positive sample budget (default 600 ≈ 30 min "
                 "CPU; 1500 ≈ 60 min CPU; bigger generally improves recall).",
        ),
    ] = 600,
    keep_cache: Annotated[
        bool,
        typer.Option(
            "--keep-cache/--no-keep-cache",
            help="Keep the per-run cache after success (debugging).",
        ),
    ] = False,
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help="Smoke run (50 samples, 2 epochs, ~2 min). The output ONNX "
                 "is NOT usable for real wake — use it to verify the pipeline.",
        ),
    ] = False,
) -> None:
    """Train a custom wake-word ONNX model on this CPU (~30 min).

    Cross-platform; CPU-only; on-demand. Behind the `[wake-train]` extra
    (`pip install opencomputer[wake-train]`). Output lands at
    `<profile_home>/wake_models/<word>.onnx` and is auto-discovered by
    `oc voice wake` on subsequent runs.

    Honest budget:
      *  `--quick`               : ~2 min (smoke; not usable)
      *  `--samples 600` (default): ~30 min cache-hit; ~35 min cold
      *  `--samples 1500`         : ~60-70 min; better recall
    """
    try:
        from opencomputer.voice.wake_train import (  # noqa: PLC0415
            TrainConfig,
            WakeTrainError,
            run_training,
        )
    except ImportError as exc:
        typer.secho(
            f"wake-train support not importable: {exc}\n"
            "install with: pip install opencomputer[wake-train]",
            fg="red", err=True,
        )
        raise typer.Exit(code=3) from exc

    profile_home = _resolve_profile_home()
    out_path = out if out is not None else (
        profile_home / "wake_models" / f"{word}.onnx"
    )

    try:
        cfg = TrainConfig(
            word=word,
            out_path=out_path,
            profile_home=profile_home,
            num_positives=samples,
            quick=quick,
            keep_cache=keep_cache,
        )
    except WakeTrainError as exc:
        typer.secho(f"config error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"training '{word}' → {out_path}\n"
        f"  positives: {samples if not quick else 50}, "
        f"voices: {cfg.num_voices}, quick: {quick}",
        fg="cyan",
    )

    def _progress(msg: str) -> None:
        typer.echo(f"  {msg}")

    try:
        result = run_training(cfg, progress=_progress)
    except KeyboardInterrupt:
        typer.secho("\ntraining cancelled by user", fg="yellow")
        raise typer.Exit(code=2)  # noqa: B904
    except WakeTrainError as exc:
        # Phase-tagged exit codes:
        #   ensure_deps → 3
        #   train (subprocess) → 4
        #   sanity → 5
        #   anything else → 1
        code = {
            "ensure_deps": 3,
            "train": 4,
            "sanity": 5,
        }.get(exc.phase, 1)
        typer.secho(
            f"training failed in phase '{exc.phase}': {exc}",
            fg="red", err=True,
        )
        raise typer.Exit(code=code) from exc
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"unexpected error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"\n✓ wrote {result.out_path}\n"
        f"  duration: {result.duration_seconds:.0f}s\n"
        f"  positives: {result.num_positives}, "
        f"negatives: {result.num_negatives}\n"
        f"  sanity check: {'ok' if result.sanity_ok else 'FAILED'}\n"
        f"  next: `oc voice wake` will auto-discover this model",
        fg="green",
    )
```

- [ ] **Step 4: Update `__all__`**

The existing `__all__ = ["voice_app"]` is still correct; the new command is registered via the decorator. No change needed.

- [ ] **Step 5: Run new tests**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/test_wake_train.py -v 2>&1 | tail -25
```
Expected: 21 passing (19 prior + 2 new).

- [ ] **Step 6: Verify CLI registered**

Run:
```bash
cd OpenComputer && python -c "from opencomputer.cli_voice import voice_app; print([c.name for c in voice_app.registered_commands])"
```
Expected: list includes `train-wake`.

- [ ] **Step 7: Run ruff**

Run:
```bash
cd OpenComputer && ruff check opencomputer/cli_voice.py tests/voice/test_wake_train.py
```
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add OpenComputer/opencomputer/cli_voice.py OpenComputer/tests/voice/test_wake_train.py
git commit -m "feat(cli): oc voice train-wake — drives the training pipeline with phase-tagged exit codes"
```

---

## Phase 5 — CHANGELOG + final verification

### Task 5.1: CHANGELOG entry

**Files:**
- Modify: `OpenComputer/CHANGELOG.md`

- [ ] **Step 1: Locate the `[Unreleased]` block**

Run:
```bash
grep -n '^## \[Unreleased\]\|^### Added' OpenComputer/CHANGELOG.md | head -5
```
Expected: a `## [Unreleased]` line with an `### Added` subheading.

- [ ] **Step 2: Insert the new bullet group at the top of `### Added`**

Edit `OpenComputer/CHANGELOG.md`. Find the first `### Added` under `[Unreleased]` and insert these bullets at the **top** of that section (above any existing content):

```markdown
- **Custom wake-word training** — `oc voice train-wake` produces a
  `hey_open_computer.onnx` (or any custom phrase) on the user's CPU in
  ~30 minutes; cross-platform (Mac + Linux primary; Windows best-effort);
  no GPU. Behind the new `[wake-train]` extra (`pip install
  opencomputer[wake-train]`). The trained ONNX lands at
  `<profile_home>/wake_models/<word>.onnx`. Verify install with
  `oc doctor wake-train`.
- **Wake-word auto-discovery** — `WakeWordDetector` now checks
  `<profile_home>/wake_models/<word>.onnx` before falling back to
  `hey_jarvis`. Closes the loop opened by PR-A's `hey_open_computer`
  default — the trained model is picked up automatically by
  `oc voice wake` on subsequent runs.
- **Honest budget in `oc voice train-wake --help`** — 30 min on CPU is
  the training step alone. First run downloads ~50MB of negative audio
  (~1 min). Sample synthesis takes ~3 min. Total: ~35 min cold,
  ~30 min cache-hit. Use `--samples 1500` (~60-70 min) for higher
  recall. `--quick` (~2 min) verifies the pipeline but the model
  isn't usable for real detection.
- **`oc doctor wake-train`** — opt-in feature preflight. Info-level
  when training deps aren't installed (so `oc doctor` exit code stays
  clean for users who don't want training).
```

- [ ] **Step 3: Run ruff (CHANGELOG isn't lintable but check the surrounding code didn't break)**

Run:
```bash
cd OpenComputer && ruff check opencomputer/ tests/voice/ 2>&1 | tail -5
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/CHANGELOG.md
git commit -m "docs(changelog): wake-train custom training entry"
```

### Task 5.2: Full local test run + cross-module sanity

**Files:** none (verification)

- [ ] **Step 1: Full voice test suite**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/ -v 2>&1 | tail -40
```
Expected: all pass — wake_word existing 14 tests + wake_word new 5 = 19; wake_train 21; tts/voice unchanged.

- [ ] **Step 2: Doctor smoke**

Run:
```bash
cd OpenComputer && python -c "from opencomputer.doctor import _check_wake_train_capable; r = _check_wake_train_capable(); print(r.status, '-', r.message)"
```
Expected: `info - wake-train deps missing: ...` (deps not installed in dev venv).

- [ ] **Step 3: CLI registration smoke**

Run:
```bash
cd OpenComputer && python -m opencomputer.cli_voice --help 2>&1 | grep -E 'train-wake|wake'
```
Expected: `train-wake` appears in the subcommand list along with `wake`.

- [ ] **Step 4: Wider regression sweep — adjacent modules**

Run:
```bash
cd OpenComputer && python -m pytest tests/voice/ tests/test_doctor_introspection_checks.py -q 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Ruff sweep across everything we touched**

Run:
```bash
cd OpenComputer && ruff check opencomputer/voice/ opencomputer/cli_voice.py opencomputer/doctor.py tests/voice/
```
Expected: clean.

- [ ] **Step 6: Final commit (only if anything changed; else skip)**

If ruff or pytest auto-fixed something or you tweaked the file, commit:

```bash
git status
# If clean, no commit. If dirty:
git add -p   # review only files in our scope
git commit -m "chore(wake-train): final sweep — ruff + tests"
```

---

## Self-review (post-write, pre-execute)

- [x] **Spec coverage:** Each spec section maps to a Task — Feature 1 (CLI) → 4.1; Feature 2 (wake_train) → 3.1-3.5; Feature 3 (auto-discovery) → 2.1; Feature 4 (doctor) → 1.2-1.3; Feature 5 (extra) → 1.1; Feature 6 (tests) → 1.3, 2.1, 3.1-3.5, 4.1; Feature 7 (CHANGELOG) → 5.1.
- [x] **No placeholders:** every step has actual code or a concrete command. No "TBD", no "implement later".
- [x] **Type consistency:**
   - `TrainConfig` fields used identically across 3.1, 3.2, 3.3, 3.4, 3.5, 4.1.
   - `WakeTrainError(message, *, phase=...)` signature consistent across all uses.
   - `run_training(cfg, *, progress=...)` signature stable.
   - `synthesize_positives(cfg, *, out_dir, progress, rng_seed=None)` keyword-only signature consistent.
   - `_auto_discover_model(word) -> Path | None` matches caller in `_resolve_word`.
   - Exit codes match between spec (1/2/3/4/5) and CLI (4.1).
- [x] **Frequent commits:** 9 commits across 5 phases (one per task), each independently testable.
- [x] **TDD ordering:** every feature task starts with the failing test, then implementation, then verification.
- [x] **Ship-with-callsite:** every new module/function has a callsite landing in the SAME PR (no orphan helpers).
- [x] **Honest scope:** acknowledged in Task 4.1 `--help` text and CHANGELOG; not hidden.
