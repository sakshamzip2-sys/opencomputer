"""Text-to-speech provider setup section."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

_TTS_OPTIONS = [
    Choice("Microsoft Edge TTS [recommended, free] - no API key needed", "edge-tts"),
    Choice("Skip - keep defaults / configure later", "skip"),
    Choice("OpenAI TTS [paid] - high quality voices", "openai"),
    Choice("xAI TTS - Grok voices, requires xAI API key", "xai"),
    Choice("ElevenLabs [paid] - premium voice quality", "elevenlabs"),
    Choice("Google Gemini TTS - prompt-controllable voices", "gemini"),
    Choice("KittenTTS [local, free] - lightweight local ONNX TTS", "kittentts"),
    Choice("Piper [local, free] - local neural TTS", "piper"),
]


def run_tts_provider_section(ctx: WizardCtx) -> SectionResult:
    idx = radiolist("Choose a provider:", _TTS_OPTIONS, default=1)
    provider = str(_TTS_OPTIONS[idx].value)
    if provider == "skip":
        return SectionResult.SKIPPED_FRESH

    tts = ctx.config.setdefault("tts", {})
    tts["provider"] = provider
    if provider == "edge-tts":
        tts.setdefault("voice", "en-US-AriaNeural")
    print(f"  ✓ Text-to-speech provider set to {provider}")
    return SectionResult.CONFIGURED
