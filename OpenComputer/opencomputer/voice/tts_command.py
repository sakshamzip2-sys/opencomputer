"""Command-type TTS provider — wire any local CLI as a TTS engine.

Wave 5 T9 — Hermes-port (2facea7f7). Users declare named providers in
config under ``tts.providers.<name>`` with ``type: command`` and a
command template; placeholders ``{input_path}``, ``{output_path}``,
``{voice}``, ``{model}``, ``{speed}``, ``{format}``, ``{text_path}``
are substituted (shell-quote-aware) before exec.

Built-in provider names cannot be shadowed by command-type entries.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass

#: Names that are reserved for built-in providers; user-declared
#: command-type entries with these names raise ValueError so the user
#: doesn't think their custom command is being invoked when in fact the
#: built-in is being used.
BUILTIN_NAMES_BLOCKED: frozenset[str] = frozenset(
    {"edge", "openai", "elevenlabs", "piper", "neutts", "kittentts"},
)

#: ``{name}`` placeholder pattern. ``{{`` / ``}}`` escape to literal braces.
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass(slots=True, frozen=True)
class CommandTTSConfig:
    """Configuration for one user-declared command-type TTS provider."""

    command: str
    output_format: str = "wav"

    @classmethod
    def from_dict(cls, d: dict) -> CommandTTSConfig:
        cmd = d.get("command")
        if not cmd:
            raise ValueError("Command-type TTS provider requires 'command'")
        return cls(command=cmd, output_format=d.get("output_format", "wav"))


def expand_placeholders(
    template: str,
    *,
    input_path: str,
    output_path: str,
    voice: str = "",
    text_path: str | None = None,
    model: str = "",
    speed: str = "",
    fmt: str = "wav",
) -> str:
    """Substitute placeholders in ``template`` with shell-quoted values.

    ``{name}`` substitutes the named value; ``{{`` / ``}}`` produce a
    literal ``{`` / ``}`` (no substitution). Substituted values run
    through ``shlex.quote`` so paths with spaces or shell metacharacters
    don't escape into the shell.
    """
    # First, swap literal-brace escapes for sentinels so the regex
    # doesn't see them as placeholders.
    s = template.replace("{{", "\x00").replace("}}", "\x01")
    mapping = {
        "input_path": input_path,
        "output_path": output_path,
        "voice": voice,
        "text_path": text_path or input_path,
        "model": model,
        "speed": speed,
        "format": fmt,
    }

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = mapping.get(key, "")
        return shlex.quote(val) if val else ""

    s = PLACEHOLDER_RE.sub(_sub, s)
    # Restore literal brace escapes.
    return s.replace("\x00", "{").replace("\x01", "}")


async def run_command_tts(
    cfg: CommandTTSConfig,
    *,
    input_path: str,
    output_path: str,
    voice: str = "",
) -> str:
    """Execute the user's command and return the output path on success."""
    cmd = expand_placeholders(
        cfg.command,
        input_path=input_path,
        output_path=output_path,
        voice=voice,
        fmt=cfg.output_format,
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command TTS failed (exit={proc.returncode}): "
            f"{err.decode(errors='replace')[:500]}",
        )
    return output_path


def validate_provider_name(name: str) -> None:
    """Reject names that would shadow a built-in provider."""
    if name in BUILTIN_NAMES_BLOCKED:
        raise ValueError(
            f"TTS provider name {name!r} is reserved for the built-in "
            f"provider; pick a different name for your command-type entry.",
        )


__all__ = [
    "BUILTIN_NAMES_BLOCKED",
    "CommandTTSConfig",
    "expand_placeholders",
    "run_command_tts",
    "validate_provider_name",
]
