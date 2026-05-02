"""TTS provider section (S2).

Modeled after Hermes's setup_tts (hermes_cli/setup.py:1262).
Independently re-implemented (no code copied).

Single 2-option radiolist:
  1. Apply Edge TTS default — writes tts.provider="edge-tts",
     voice="en-US-AriaNeural". Free, cloud-based, no API key needed.
  2. Skip — keep current.

Apply path is a focused merge: only `provider` and `voice` are
overwritten; other tts.* keys (speed, model, custom voice maps) are
preserved.

Picking among offline engines (NeuTTS, KittenTTS) and premium engines
(ElevenLabs, OpenAI TTS, xAI, MiniMax, Mistral, Gemini) is deferred —
those need dependency-install or per-provider auth logic; users can
edit ~/.opencomputer/config.yaml or run `oc setup tts` (planned) for
fine-grained control.

Default matches Hermes's choice (edge) per
hermes_cli/setup.py::_setup_tts_provider — Edge TTS works out of the
box without any signup.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

_DEFAULT_TTS = {
    "provider": "edge-tts",
    "voice": "en-US-AriaNeural",
}


def _apply_defaults(ctx: WizardCtx) -> None:
    """Merge default tts.provider/voice into config without clobbering
    other tts.* keys."""
    tts_block = ctx.config.setdefault("tts", {})
    for key, value in _DEFAULT_TTS.items():
        tts_block[key] = value


def run_tts_provider_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("Apply Edge TTS default (free, no API key)", "apply"),
        Choice("Skip — configure later", "skip"),
    ]
    idx = radiolist(
        "Configure TTS provider for voice output?",
        choices, default=1,
        description="Default uses Edge TTS — Microsoft's free cloud voices, "
                     "no signup. Premium engines (ElevenLabs, OpenAI, xAI, "
                     "MiniMax, Mistral, Gemini, NeuTTS, KittenTTS) "
                     "configurable via ~/.opencomputer/config.yaml.",
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    _apply_defaults(ctx)
    print("  ✓ Applied Edge TTS default (no API key required).")
    return SectionResult.CONFIGURED
