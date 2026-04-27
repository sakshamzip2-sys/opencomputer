"""``opencomputer voice`` CLI — manual TTS / STT smoke and one-off use.

Subcommands:

    opencomputer voice synthesize "text" [-o OUT.ogg]   — TTS to file
    opencomputer voice transcribe FILE                   — STT to stdout
    opencomputer voice cost-estimate "text" [...]        — projected USD without calling

Real cost-guard checks apply on synthesize / transcribe (see ``opencomputer cost``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opencomputer.cost_guard import BudgetExceeded
from opencomputer.voice import (
    VoiceConfig,
    stt_cost_usd,
    synthesize_speech,
    transcribe_audio,
    tts_cost_usd,
)

voice_app = typer.Typer(
    name="voice",
    help="Text-to-speech and speech-to-text via OpenAI (cost-guarded).",
    no_args_is_help=True,
)


@voice_app.command("synthesize")
def voice_synthesize(
    text: Annotated[str, typer.Argument(help="Text to speak.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Path to write the audio file (default: temp file).")
    ] = None,
    model: Annotated[str, typer.Option("--model", help="tts-1 (default) or tts-1-hd.")] = "tts-1",
    voice: Annotated[str, typer.Option("--voice", help="alloy / echo / fable / onyx / nova / shimmer.")] = "alloy",
    fmt: Annotated[str, typer.Option("--format", help="opus (default — Telegram) / mp3 / wav / flac / aac / pcm.")] = "opus",
) -> None:
    """Synthesize speech from text and save it to a file."""
    cfg = VoiceConfig(model=model, voice=voice, format=fmt)
    try:
        out_path = synthesize_speech(
            text, cfg=cfg, dest_dir=output.parent if output else None
        )
    except BudgetExceeded as exc:
        typer.secho(f"Blocked by cost-guard: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except (ValueError, RuntimeError) as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc

    if output and output.resolve() != out_path.resolve():
        # User specified a particular output filename; rename the synthesized file.
        out_path.rename(output)
        out_path = output

    cost = tts_cost_usd(text, model=model)
    typer.secho(f"Synthesized → {out_path}", fg="green")
    typer.echo(f"  chars:  {len(text)}")
    typer.echo(f"  cost:   ${cost:.4f}")


@voice_app.command("transcribe")
def voice_transcribe(
    audio: Annotated[Path, typer.Argument(help="Audio file path.")],
    model: Annotated[str, typer.Option("--model", help="Default whisper-1.")] = "whisper-1",
    language: Annotated[
        str | None,
        typer.Option("--language", "-l", help="ISO-639-1 hint (e.g. en, hi). Optional."),
    ] = None,
) -> None:
    """Transcribe an audio file to text."""
    try:
        text = transcribe_audio(audio, model=model, language=language)
    except BudgetExceeded as exc:
        typer.secho(f"Blocked by cost-guard: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except (ValueError, RuntimeError) as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc

    typer.echo(text)


@voice_app.command("cost-estimate")
def voice_cost_estimate(
    text: Annotated[str | None, typer.Option("--text", "-t", help="Text to project TTS cost for.")] = None,
    duration: Annotated[
        float | None,
        typer.Option("--duration", "-d", help="Audio duration in seconds for STT projection."),
    ] = None,
    model: Annotated[str, typer.Option("--model", help="tts-1 / tts-1-hd / whisper-1.")] = "tts-1",
) -> None:
    """Estimate USD cost without making an API call.

    Pass --text for TTS or --duration for STT (or both).
    """
    if text is None and duration is None:
        typer.secho("Error: pass --text and/or --duration", fg="red", err=True)
        raise typer.Exit(2)

    if text is not None:
        cost = tts_cost_usd(text, model=model)
        typer.echo(f"TTS  ({model:8s})  {len(text):5d} chars   = ${cost:.4f}")
    if duration is not None:
        cost = stt_cost_usd(duration, model=model if model.startswith("whisper") else "whisper-1")
        typer.echo(f"STT  (whisper-1)   {duration:5.1f}s        = ${cost:.4f}")


@voice_app.command("talk")
def voice_talk(
    prefer_local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Prefer local STT backends (mlx-whisper / whisper-cpp) over the OpenAI API.",
        ),
    ] = False,
) -> None:
    """Enter continuous push-to-talk voice mode.

    Press Enter to start a recording, Enter again to stop and send. Ctrl+C exits.
    Real spacebar push-to-talk + barge-in keyboard handling lands in a polish PR.
    """
    import asyncio

    from extensions.voice_mode.voice_mode import run_voice_loop

    from opencomputer.agent.config import _home
    from opencomputer.cost_guard.guard import get_default_guard

    typer.echo("🎤 voice-mode: starting (Enter to record / Enter to stop, Ctrl+C to exit)")

    # Stub agent for T5 — T6+ wires the real AgentLoop.
    async def stub_agent(user_text: str) -> str:
        return f"You said: {user_text}. (real agent integration lands in a follow-up)"

    def record_trigger() -> bool:
        # Block until the user presses Enter twice (start + stop).
        # The orchestrator wraps the AudioCapture lifecycle around this call,
        # so the second prompt acts as the "stop" edge.
        input("Press Enter to record (then Enter to stop): ")
        return True

    def stop_trigger() -> bool:
        return False  # never stop autonomously; user exits via Ctrl+C

    try:
        asyncio.run(
            run_voice_loop(
                agent_runner=stub_agent,
                cost_guard=get_default_guard(),
                profile_home=_home(),
                record_trigger=record_trigger,
                stop_trigger=stop_trigger,
                prefer_local_stt=prefer_local,
            )
        )
    except KeyboardInterrupt:
        typer.echo("\nvoice-mode: stopped")


__all__ = ["voice_app"]
