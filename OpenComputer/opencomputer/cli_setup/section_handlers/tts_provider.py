"""TTS provider section (S2).

Modeled after Hermes's setup_tts (hermes_cli/setup.py:1262).
Independently re-implemented (no code copied).

Single 2-option radiolist:
  1. Apply TTS defaults — writes tts.provider="openai-tts", voice="alloy".
     Reuses OPENAI_API_KEY env var; no extra setup needed if user has
     an OpenAI key.
  2. Skip — keep current.

Apply path is a focused merge: only `provider` and `voice` are
overwritten; other tts.* keys (speed, model, custom voice maps) are
preserved.

Picking among offline engines (NeutTTS, KittenTTS, eSpeak-NG) and
ElevenLabs / fine-grained voice config is deferred — those need
dependency-install logic; users can edit ~/.opencomputer/config.yaml
or run `oc tts setup` (planned) for fine-grained control.
"""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

_DEFAULT_TTS = {
    "provider": "openai-tts",
    "voice": "alloy",
}


def _apply_defaults(ctx: WizardCtx) -> None:
    """Merge default tts.provider/voice into config without clobbering
    other tts.* keys."""
    tts_block = ctx.config.setdefault("tts", {})
    for key, value in _DEFAULT_TTS.items():
        tts_block[key] = value


def run_tts_provider_section(ctx: WizardCtx) -> SectionResult:
    choices = [
        Choice("Apply TTS defaults (openai-tts, voice=alloy)", "apply"),
        Choice("Skip — configure later", "skip"),
    ]
    idx = radiolist(
        "Configure TTS provider for voice output?",
        choices, default=1,
        description="Default uses OpenAI TTS. Offline engines "
                     "(NeutTTS, KittenTTS, eSpeak-NG) and ElevenLabs are "
                     "configurable via ~/.opencomputer/config.yaml.",
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    _apply_defaults(ctx)
    print("  ✓ Applied TTS defaults: openai-tts, voice=alloy")
    print("  Set OPENAI_API_KEY in env to use voice output.")
    return SectionResult.CONFIGURED
