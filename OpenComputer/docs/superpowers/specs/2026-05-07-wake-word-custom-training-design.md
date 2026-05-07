# Custom hey_open_computer Wake-Word Training — Design Spec

**Date:** 2026-05-07
**Branch:** `worktree-pr-a-steer-wake-acp-2026-05-07` (extends PR-A; lands as a follow-up commit on the same worktree)
**Driver:** PR-A (ed93f859) shipped `hey_open_computer` as the conceptual default but with **silent fallback to `hey_jarvis`** because no custom ONNX is bundled. The original spec (`docs/superpowers/specs/2026-05-07-pr-a-steer-wake-acp-design.md`, line 408) explicitly deferred custom-word training: *"Voice Wake custom-word training — defer; openWakeWord supports custom words but training is its own UX."* This spec un-defers that work.

## Why this scope

A user who installs `opencomputer[wake]` today and runs `oc voice wake` is told the truth — *"requested 'hey_open_computer' is not bundled and no `--model` was provided. Falling back to 'hey_jarvis'."* — and pointed at the openWakeWord training pipeline. **But that pipeline is Linux/CUDA-leaning, takes a Jupyter notebook to run, and depends on `piper-sample-generator` (Linux-only).** Saksham is on macOS. The closing-the-loop work is to ship a one-shot CLI that produces the missing ONNX on the user's own laptop in ~30 minutes, on-demand.

Three design constraints non-negotiable:

1. **Cross-platform.** Must run on macOS (the user's environment) and Linux. Windows is best-effort.
2. **CPU-only.** No CUDA, no Apple Silicon Metal — pure-CPU training. The model is small enough that this is realistic.
3. **On-demand.** Triggered ONLY by an explicit CLI command. Never automatic. Never during install. Never in CI.

## Scope (this commit)

### Feature 1 — `oc voice train-wake` CLI

**File:** `opencomputer/cli_voice.py` (extend with new Typer command).

```python
@voice_app.command("train-wake")
def voice_train_wake(
    word: str = typer.Option("hey_open_computer", "--word",
        help="Wake-word phrase to train (underscore-separated; lowercase)."),
    out: Path | None = typer.Option(None, "--out",
        help="Output ONNX path. Default: <profile_home>/wake_models/<word>.onnx"),
    samples: int = typer.Option(600, "--samples", min=100, max=5000,
        help="Number of synthetic positive samples (default 600; bigger = "
             "longer training, generally better recall)."),
    keep_cache: bool = typer.Option(False, "--keep-cache",
        help="Keep the per-run tempdir after success (debugging)."),
    quick: bool = typer.Option(False, "--quick",
        help="Smoke mode — 50 samples, 2 epochs. Verifies the pipeline runs "
             "end-to-end. Output model is NOT usable for real wake."),
) -> None: ...
```

Exit codes:

| code | meaning |
|---|---|
| 0 | success — ONNX written, sanity check passed |
| 1 | unknown error (caught with traceback) |
| 2 | user cancelled (Ctrl+C) |
| 3 | training deps missing — points at `pip install opencomputer[wake-train]` |
| 4 | upstream openwakeword crash — preserves the run's tempdir + prints location |
| 5 | sanity check failed (ONNX written but Model() can't load it) |

### Feature 2 — `opencomputer/voice/wake_train.py` (new module, ~400 LOC)

**Public API:**

```python
class WakeTrainError(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class TrainConfig:
    word: str
    out_path: Path
    num_positives: int = 600
    num_voices: int = 4               # how many Piper voices to round-robin
    quick: bool = False
    keep_cache: bool = False
    profile_home: Path                # for cache + output dir resolution

@dataclass(frozen=True, slots=True)
class TrainResult:
    out_path: Path                    # written ONNX
    duration_seconds: float
    num_positives: int
    num_negatives: int
    sanity_ok: bool                   # Model() loaded + 80ms silence inferred without crashing
    cache_dir: Path                   # tempdir; deleted on success unless keep_cache=True

def run_training(cfg: TrainConfig, *,
                 progress: Callable[[str], None] | None = None) -> TrainResult:
    """End-to-end training pipeline.
    
    Phases (in order):
      1. ensure_deps()                   ~instant
      2. synthesize_positives(cfg)       ~3 min for 600 samples
      3. ensure_negatives(cfg)           ~1 min cold, ~0 cached
      4. write_training_config(cfg)      <1s
      5. invoke_openwakeword_train(cfg)  ~25 min (the budget)
      6. sanity_check_onnx(out_path)     ~2s
      7. atomic_rename(tmp -> final)     <1s
    
    Total: ~30 min cache-hit, ~31 min cold first run.
    Raises WakeTrainError with phase-tagged context on failure.
    """
```

**Internal phases:**

#### Phase 2.1 — Positive sample synthesis (cross-platform, replaces piper-sample-generator)

```python
_DEFAULT_VOICES = (
    "en_US-lessac-medium",        # neutral male-leaning
    "en_US-amy-medium",           # female-leaning
    "en_GB-jenny_dioco-medium",   # British female
    "en_GB-ryan-medium",          # British male
)

def synthesize_positives(cfg: TrainConfig, *, progress) -> list[Path]:
    """Generate ``cfg.num_positives`` WAV files of the wake phrase.
    
    Variation budget per sample (uniform random):
        length_scale  ∈ [0.85, 1.15]    speed jitter ±15%
        noise_scale   ∈ [0.55, 0.75]    voice variation
        noise_w_scale ∈ [0.65, 0.95]    pronunciation variation
    
    Voices are round-robin'd across cfg.num_voices to cover male/female
    and US/UK accents. The phrase is rendered from cfg.word with
    underscores replaced by spaces ("hey_open_computer" -> "hey open
    computer").
    
    Output: list of paths under cache_dir/positives/*.wav (16 kHz mono).
    """
```

#### Phase 2.2 — Negative-sample provisioning

```python
def ensure_negatives(cfg: TrainConfig, *, progress) -> Path:
    """Ensure a cached pool of negative audio exists.
    
    First run: downloads a ~50MB curated slice of audio clips from
    HuggingFace dataset 'dscripka/audioset_500m_train_clips' into
    <profile_home>/cache/wake_train/_negatives/. ~3500 clips, 16 kHz
    mono. Cached forever after.
    
    Subsequent runs: returns the cache dir directly.
    
    Returns: Path to the negatives dir.
    """
```

If HuggingFace is unreachable, fall back to a documented manual path: user runs `oc voice train-wake --negatives-dir <PATH>` (deferred — out of v1 scope).

#### Phase 2.3 — Training config YAML

We write the openwakeword-format YAML the upstream `train.py` expects, with our paths plugged in:

```yaml
target_phrase: ["hey open computer"]
model_name: "hey_open_computer"
n_samples: 600
n_samples_val: 100
batch_size: 64
epochs: 30                       # ~25 min CPU; --quick reduces to 2
learning_rate: 0.0001
target_accuracy: 0.7
target_recall: 0.5
target_false_positives_per_hour: 0.5
positive_clips_path: "<cache>/positives"
negative_clips_path: "<cache>/negatives"
output_dir: "<cache>/model_output"
```

#### Phase 2.4 — Training subprocess

```python
def invoke_openwakeword_train(cfg, config_yaml: Path, *, progress) -> Path:
    """Run `python -m openwakeword.train --train_model --config_path ...`.
    
    Streams subprocess stdout live to ``progress`` so the user sees epoch
    counters. SIGINT propagates to the child via ``proc.send_signal``.
    Returns the path to the trained ONNX inside the cache dir.
    
    Raises WakeTrainError with the subprocess return code + tail of
    stderr on non-zero exit.
    """
```

#### Phase 2.5 — Sanity check + atomic rename

```python
def sanity_check_onnx(onnx_path: Path) -> bool:
    """Load the ONNX through openwakeword.Model and predict on 80ms of
    silence. Verifies the file is well-formed without needing real audio.
    Returns True if Model() init + one predict() succeed.
    """

def atomic_rename(src: Path, dst: Path) -> None:
    """tmp -> final on the same FS. Creates parent dirs."""
```

### Feature 3 — Auto-discovery in existing `WakeWordDetector`

**File:** `opencomputer/voice/wake_word.py` (~10 LOC patch in `_resolve_word`).

Currently the detector falls back to `hey_jarvis` whenever `model_path` is None and the requested word isn't in `BUNDLED_WAKE_WORDS`. We add a check **before** the fallback:

```python
def _resolve_word(self) -> str:
    if self.model_path is not None:
        ...  # unchanged
    if self.word in BUNDLED_WAKE_WORDS:
        ...  # unchanged
    # NEW: check for an auto-discovered trained model in the well-known
    # location before falling back.
    auto_path = _auto_discover_model(self.word)
    if auto_path is not None:
        _log.info("wake: auto-discovered trained model at %s", auto_path)
        self.model_path = auto_path
        self._effective_word = self.word
        self._fell_back = False
        return self.word
    # ... existing fallback to hey_jarvis ...

def _auto_discover_model(word: str) -> Path | None:
    """Look for <profile_home>/wake_models/<word>.onnx.
    
    profile_home is resolved via the same logic the CLI uses (active
    profile, falling back to ~/.opencomputer/default).
    """
```

This closes the loop: after a user runs `oc voice train-wake`, future `oc voice wake` runs (without `--model`) automatically use the trained model.

### Feature 4 — Doctor probe

**File:** `opencomputer/doctor.py` (~30 LOC, new check).

```python
def _check_wake_train_capable() -> CheckResult:
    """Verify the [wake-train] extra is installable on this platform.
    
    Imports: torch, openwakeword.train, huggingface_hub, soundfile, piper.
    Reports info-level (not fail) if any are missing — training is opt-in.
    """
```

Wired into the existing `oc doctor` registry alongside `_check_wake_word_capable`.

### Feature 5 — `[wake-train]` extra

**File:** `pyproject.toml`.

```toml
[project.optional-dependencies]
# PR follow-up (2026-05-07): custom wake-word training pipeline.
#   pip install opencomputer[wake-train]
# Heavy: pulls torch + openwakeword's training extras + huggingface_hub.
# Cross-platform; CPU-only; ~30 min training run on a modern CPU.
# Verify install: oc doctor wake-train. Run training: oc voice train-wake.
wake-train = [
    "openwakeword[train]>=0.6.0,<0.7",
    "torch>=2.1",
    "huggingface_hub>=0.20",
    "soundfile>=0.12",
    "piper-tts>=1.2",
    # `wake` is the runtime dep set; training implies runtime.
    "openwakeword>=0.6.0",
    "onnxruntime>=1.17",
]
```

### Feature 6 — Tests

**File:** `tests/voice/test_wake_train.py` (new, ~250 LOC).

Test ladder:

| level | test | mocks |
|---|---|---|
| unit | `test_train_config_defaults` | none |
| unit | `test_synthesize_positives_uses_round_robin_voices` | mock piper.synthesize_wav |
| unit | `test_synthesize_positives_applies_prosody_jitter` | mock piper.synthesize_wav, assert kwargs vary across calls |
| unit | `test_ensure_negatives_uses_cache_when_present` | mock huggingface_hub.snapshot_download |
| unit | `test_ensure_negatives_downloads_when_cold` | mock snapshot_download — assert called with the right repo_id |
| unit | `test_write_training_config_yaml_shape` | reads back the YAML, asserts schema |
| unit | `test_invoke_openwakeword_train_streams_progress` | mock subprocess.Popen — assert progress callback fired per stdout line |
| unit | `test_invoke_openwakeword_train_propagates_sigint` | mock Popen, send signal, assert send_signal called |
| unit | `test_sanity_check_handles_corrupt_onnx` | write garbage to a .onnx file, assert returns False |
| unit | `test_atomic_rename_creates_parent_dirs` | filesystem |
| unit | `test_run_training_quick_path` | full mock: piper, hf, openwakeword.train CLI — assert end-to-end orchestration |
| unit | `test_run_training_writes_to_atomic_temp_then_renames` | filesystem assertion: tmp file exists between phases, gone after |
| integ | `test_auto_discovery_finds_trained_model` | filesystem: write a fake ONNX at expected path; init WakeWordDetector; assert it picks the path up |
| integ | `test_auto_discovery_falls_back_when_no_model` | filesystem: empty dir; assert fallback to hey_jarvis still fires |
| cli | `test_cli_train_wake_missing_deps_exit_code_3` | monkeypatch sys.modules["openwakeword"] = None |
| cli | `test_cli_train_wake_quick_smoke` | runs the full pipeline with quick=True against mocked subprocess; asserts ONNX file exists at expected path post-run |
| doctor | `test_doctor_wake_train_capable_when_all_deps_present` | mock imports |
| doctor | `test_doctor_wake_train_info_when_missing` | mock ImportError |

**No real-CPU 30-min tests.** All slow paths mocked. The "real" smoke is left to the user's manual run + a `@pytest.mark.slow` opt-in test gated on `WAKE_TRAIN_REAL=1` env var.

### Feature 7 — CHANGELOG

`OpenComputer/CHANGELOG.md` under `[Unreleased]`:

```markdown
- **Custom wake-word training** — `oc voice train-wake` produces a custom
  ONNX model for `hey_open_computer` (or any phrase) on the user's CPU
  in ~30 minutes. Cross-platform (Mac/Linux); no GPU needed. Trained
  model lands at `<profile_home>/wake_models/<word>.onnx` and is
  auto-discovered by `oc voice wake` — no `--model` flag needed on
  subsequent runs. Behind the new `[wake-train]` extra (heavy: pulls
  torch + openwakeword[train] + huggingface_hub). Verify install with
  `oc doctor wake-train`.
- **Honest scope:** the 30-minute budget is for the training step alone.
  First run downloads ~50MB of negative audio (~1 min). Sample synthesis
  takes ~3 min. Total cold: ~35 min; cache-hit: ~30 min. `--quick` runs
  a smoke pipeline (~2 min) but the model is not usable.
- **Wake-word auto-discovery** — `WakeWordDetector` now checks
  `<profile_home>/wake_models/<word>.onnx` before falling back to
  `hey_jarvis`. Closes the loop opened by PR-A's `hey_open_computer`
  fallback hint.
```

## Ship-with-callsite checklist

| Module / function | Callsite |
|---|---|
| `wake_train.run_training()` | `cli_voice.py::voice_train_wake` |
| `wake_train.synthesize_positives()` | `wake_train.run_training()` |
| `wake_train.ensure_negatives()` | `wake_train.run_training()` |
| `wake_train.invoke_openwakeword_train()` | `wake_train.run_training()` |
| `wake_train.sanity_check_onnx()` | `wake_train.run_training()` |
| `_auto_discover_model()` | `WakeWordDetector._resolve_word()` |
| `_check_wake_train_capable()` | `doctor.py` registry (alongside existing `_check_wake_word_capable`) |
| `[wake-train]` extra | `pyproject.toml` |

## Risk register (post-audit)

| # | Finding | Disposition |
|---|---|---|
| A1 | openwakeword `Trainer` class is internal — version-volatile | Subprocess CLI; pin `openwakeword>=0.6,<0.7` |
| A2 | `piper-sample-generator` is Linux-only | Replace with our own `tts_piper.py` driver — proven cross-platform |
| A3 | "30-min CPU" is best-case | Honest range in `--help` + CHANGELOG: "30–50 min cold" |
| A4 | HuggingFace fetch can fail | Cache to `<profile_home>/cache/wake_train/_negatives/`; v1 hard-errors with phase-tagged exit code, deferred manual path doc |
| A5 | Mid-training Ctrl+C | SIGINT propagates to child; cache preserved with `--keep-cache`; tempdir always preserved on failure |
| A6 | Model trained but ONNX corrupt | Sanity-check loads via `openwakeword.Model` + 80ms silence predict |
| A7 | Atomic rename across FS boundary | Write tmp into the same dir as final; rename is intra-FS, safe |
| A8 | torch dep size (~2GB) | Behind opt-in extra; user explicitly opts in via `pip install opencomputer[wake-train]` |
| A9 | First-run network requirement | Documented in CLI `--help` + CHANGELOG |
| A10 | Auto-discovery surprises a user who set `--model` | `--model` takes precedence (already does); auto-discovery only fires when `--model` is None AND word is not bundled |
| A11 | Long-running subprocess on CI | All real-training tests gated by `WAKE_TRAIN_REAL=1`; default test run mocks subprocess |
| A12 | Trained model fails real-world acoustic tests | **Accepted as risk** — quality of a 30-min CPU model is best-effort. User can re-train with `--samples 1500` for better recall. |
| B1 | Profile-home resolution diverges between training and detection | Both use the same `_home()` / `read_active_profile` chain; a single helper `_wake_models_dir(profile_home)` is shared |
| B2 | Concurrent `oc voice train-wake` invocations | PID-file singleton at `<profile_home>/voice_wake_train.pid` (mirrors existing `voice_wake.pid` pattern) |
| C1 | Windows support | **Best-effort** — torch + onnx work on Windows; piper-tts works on Windows; openwakeword[train] is Linux-tested only. v1 documents "macOS + Linux supported; Windows best-effort." |

## Out of scope (deferred — explicit)

- **Multi-word batch training** in one run (`--word foo --word bar`) — single-word per invocation is enough.
- **Custom voice list** override (`--voices "custom1,custom2"`) — the 4-voice default covers most accents.
- **TFLite export** for Home Assistant on-device deployment — ONNX is sufficient for OpenComputer.
- **INT8 quantization** — ONNX float32 is small enough (<200KB) and fast on CPU.
- **Hyperparameter search / sweep** — defaults are openwakeword's recommended values.
- **GPU acceleration** — explicit non-goal; we lean on CPU for portability.
- **Manual `--negatives-dir <PATH>`** offline path — defer until a user reports HuggingFace blocked.
- **Adversarial fine-tuning loop** (false-positive correction from real audio) — separate feature; covered by `openwakeword`'s docs/`custom_verifier_models.md` if needed.

## Self-review (pre-implementation)

- [x] No "TBD" / vague placeholders.
- [x] Each module has a documented callsite.
- [x] Tests scoped per phase; total ~18 tests (16 unit + 2 integration).
- [x] Risk register integrates audit findings; failure modes mapped.
- [x] Cross-platform stance honest (macOS + Linux primary; Windows best-effort).
- [x] All new deps gated behind `[wake-train]` extra.
- [x] No new SQLite tables / schema migration.
- [x] Honest scope: 30–50 min cold, 30–40 min cache-hit.
- [x] Composability with PR-A: auto-discovery patch is 5 LOC and additive.
- [x] Default OFF (CLI invocation required).
- [x] No CI impact (no tests run training; `WAKE_TRAIN_REAL=1` gate for real run).
