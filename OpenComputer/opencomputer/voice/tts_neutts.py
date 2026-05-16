"""NeuTTS provider — local neural text-to-speech with voice cloning.

Milestone 4. NeuTTS (the ``neutts`` package, ``github.com/neuphonic/neutts``)
is an on-device TTS speech-language model: it synthesizes speech *in the voice
of a reference clip* — voice cloning, not a fixed voice set. It runs fully
locally, with no network call at synthesis time and no per-call cost — which
is the point of this provider versus the OpenAI-backed
:func:`opencomputer.voice.synthesize_speech`.

``neutts`` is a heavy ML dependency (it pulls ``torch`` + ``transformers``), so
it lives behind the ``[neutts]`` optional extra and is **lazy-imported** — a
user without the extra pays no import cost and OC behaves exactly as before.
:class:`~opencomputer.tools.voice_synthesize_local.VoiceSynthesizeLocalTool` is
registered only when :func:`neutts_available` is true.

Unlike :class:`opencomputer.voice.tts_piper.PiperTTS` (a *fixed-voice* local
TTS), NeuTTS **requires a reference audio clip** — 3-15 s of clean, continuous
mono speech — plus that clip's transcript. The synthesized speech mimics the
reference speaker; there is no built-in default voice.

The API surface mirrored here (``NeuTTS`` / ``encode_reference`` / ``infer``)
is the one published in the ``neuphonic/neutts`` README for the pinned
``neutts>=1.2.1`` range; a live synthesis run is the final validation and is
deferred (the dependency is not installed in CI).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Default backbone — the GGUF-quantized NeuTTS Air model. GGUF runs on CPU
#: (via ``llama-cpp-python``) without a GPU, the smallest practical default.
DEFAULT_BACKBONE_REPO: str = "neuphonic/neutts-air-q4-gguf"

#: Default neural audio codec repo (NeuCodec — a single-codebook codec).
DEFAULT_CODEC_REPO: str = "neuphonic/neucodec"

#: NeuTTS emits 24 kHz mono audio — the sample rate written into the WAV.
NEUTTS_SAMPLE_RATE: int = 24_000


@dataclass(slots=True, frozen=True)
class NeuTTSConfig:
    """Knobs for a NeuTTS synthesis call.

    ``reference_audio`` + ``reference_text`` are the voice to clone — NeuTTS
    has no fixed voice, so every synthesis mimics a reference speaker.

    Attributes:
        reference_audio: Path to a reference ``.wav`` — 3-15 s of clean,
            continuous mono speech (16-44 kHz). The synthesized voice clones
            this speaker.
        reference_text: The exact transcript of ``reference_audio``. NeuTTS
            uses it to align the reference; a wrong transcript degrades the
            clone.
        backbone_repo: HuggingFace repo id of the backbone LM.
        codec_repo: HuggingFace repo id of the neural audio codec.
        device: ``"cpu"`` (default) or a CUDA device string.
    """

    reference_audio: str
    reference_text: str
    backbone_repo: str = DEFAULT_BACKBONE_REPO
    codec_repo: str = DEFAULT_CODEC_REPO
    device: str = "cpu"


def neutts_available() -> bool:
    """Return whether the optional ``neutts`` package is importable.

    Cheap and side-effect-free — an :mod:`importlib` spec lookup, no import of
    the (heavy) package body. Used to gate ``VoiceSynthesizeLocalTool``
    registration in ``cli._register_builtin_tools``: absent the extra, the
    tool is never registered and the agent never reaches for it.
    """
    try:
        return importlib.util.find_spec("neutts") is not None
    except (ImportError, ValueError):
        # ImportError: importlib machinery unavailable (never in practice).
        # ValueError: a partially-installed ``neutts`` with a broken spec.
        return False


def _import_neutts():
    """Lazy-import ``neutts``; raise an actionable error when it is missing."""
    try:
        import neutts  # noqa: PLC0415 — lazy by design (heavy optional dep)
    except ImportError as exc:
        raise RuntimeError(
            "NeuTTS local voice synthesis requires the 'neutts' package. "
            "Install it with: pip install opencomputer[neutts]",
        ) from exc
    return neutts


@lru_cache(maxsize=2)
def _load_model(backbone_repo: str, codec_repo: str, device: str):
    """Load + cache a ``NeuTTS`` model keyed on ``(backbone, codec, device)``.

    Loading is expensive — the weights download from HuggingFace on first use
    and the model initialises ``torch`` — so the instance is cached; repeat
    synthesis is then fast. The cache is capped small to bound memory.
    """
    neutts = _import_neutts()
    logger.info(
        "Loading NeuTTS model (backbone=%s, codec=%s, device=%s) — "
        "first use downloads weights from HuggingFace",
        backbone_repo,
        codec_repo,
        device,
    )
    return neutts.NeuTTS(
        backbone_repo=backbone_repo,
        backbone_device=device,
        codec_repo=codec_repo,
        codec_device=device,
    )


class NeuTTSSynthesizer:
    """Synthesize text to a WAV file via local NeuTTS, cloning a reference voice.

    Construct once per :class:`NeuTTSConfig`; :meth:`synthesize` is reentrant.
    Each call runs the (blocking) model on a worker thread so the agent loop's
    event loop is never blocked — the same discipline
    :class:`opencomputer.voice.tts_piper.PiperTTS` follows.
    """

    def __init__(self, config: NeuTTSConfig) -> None:
        self.config = config

    async def synthesize(self, text: str, *, out_path: str) -> str:
        """Render ``text`` into a 24 kHz WAV at ``out_path`` in the reference voice.

        Args:
            text: The text to speak. Must be non-empty.
            out_path: Where to write the ``.wav``. Parent dirs are created.

        Returns:
            ``out_path`` — the path written.

        Raises:
            ValueError: ``text`` is empty.
            FileNotFoundError: ``reference_audio`` does not exist.
            RuntimeError: ``neutts`` is not installed.
        """
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        ref_audio = self.config.reference_audio
        if not ref_audio or not Path(ref_audio).is_file():
            raise FileNotFoundError(
                f"NeuTTS reference audio not found: {ref_audio!r}"
            )
        # The whole NeuTTS pipeline (encode reference + infer) is synchronous
        # and CPU-heavy — push it to a worker thread.
        await asyncio.to_thread(self._synthesize_blocking, text, out_path)
        return out_path

    def _synthesize_blocking(self, text: str, out_path: str) -> None:
        """Run the blocking NeuTTS pipeline. Executed on a worker thread."""
        import soundfile as sf  # noqa: PLC0415 — only needed on the synth path

        model = _load_model(
            self.config.backbone_repo,
            self.config.codec_repo,
            self.config.device,
        )
        ref_codes = model.encode_reference(self.config.reference_audio)
        wav = model.infer(text, ref_codes, self.config.reference_text)
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(dest), wav, NEUTTS_SAMPLE_RATE)
        logger.info("NeuTTS synthesized %d chars → %s", len(text), dest)


def download_neutts_model(
    *,
    backbone_repo: str = DEFAULT_BACKBONE_REPO,
    codec_repo: str = DEFAULT_CODEC_REPO,
    device: str = "cpu",
) -> None:
    """Pre-download + warm the NeuTTS model weights.

    Instantiating ``NeuTTS`` downloads the backbone + codec from HuggingFace on
    first use; calling this ahead of time front-loads that one-time cost.
    Used by ``oc voice install-neutts``. Raises :class:`RuntimeError` when the
    ``neutts`` package is not installed.
    """
    _load_model(backbone_repo, codec_repo, device)


__all__ = [
    "DEFAULT_BACKBONE_REPO",
    "DEFAULT_CODEC_REPO",
    "NEUTTS_SAMPLE_RATE",
    "NeuTTSConfig",
    "NeuTTSSynthesizer",
    "download_neutts_model",
    "neutts_available",
]
