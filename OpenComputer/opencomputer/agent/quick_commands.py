"""Zero-token quick commands.

Hermes-CLI parity (doc lines 113-134). Loaded from
``~/.opencomputer/config.yaml`` under ``quick_commands:``. Two types:

- ``exec``  — run a shell command, return captured stdout/stderr.
- ``alias`` — re-dispatch through the slash command path.

Quick commands are checked BEFORE slash dispatch so they can shadow a
slash name. Alias depth is capped at 5 to prevent A→B→A loops.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_TIMEOUT_DEFAULT = 30.0  # seconds — Hermes parity (doc line 130)
_MAX_OUTPUT = 4096  # chars
_MAX_ALIAS_DEPTH = 5


class QuickCommandError(Exception):
    """Raised when a quick command can't run (alias loop, malformed config)."""


@dataclass(frozen=True)
class QuickCommandSpec:
    type: str  # "exec" | "alias"
    command: str = ""  # for type=exec
    target: str = ""  # for type=alias (e.g. "/usage")


@dataclass
class QuickResult:
    output: str
    timed_out: bool = False
    depth: int = 0


@dataclass
class QuickCommands:
    """Loaded quick-command map plus run-orchestration."""

    commands: dict[str, QuickCommandSpec] = field(default_factory=dict)
    timeout: float = _TIMEOUT_DEFAULT
    dispatcher: Callable[[str, str, int], QuickResult] | None = None

    def __contains__(self, name: str) -> bool:
        return name in self.commands

    def __getitem__(self, name: str) -> QuickCommandSpec:
        return self.commands[name]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        timeout: float = _TIMEOUT_DEFAULT,
        dispatcher: Callable[[str, str, int], QuickResult] | None = None,
    ) -> QuickCommands:
        try:
            raw: dict[str, Any] = (
                yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            )
        except FileNotFoundError:
            return cls(commands={}, timeout=timeout, dispatcher=dispatcher)
        section: dict[str, Any] = raw.get("quick_commands", {}) or {}
        commands: dict[str, QuickCommandSpec] = {}
        for name, spec in section.items():
            if not isinstance(spec, dict):
                continue
            t = spec.get("type", "exec")
            commands[name] = QuickCommandSpec(
                type=t,
                command=str(spec.get("command", "")),
                target=str(spec.get("target", "")),
            )
        return cls(commands=commands, timeout=timeout, dispatcher=dispatcher)

    def run(
        self, name: str, args: str, *, _depth: int = 0
    ) -> QuickResult | None:
        spec = self.commands.get(name)
        if spec is None:
            return None
        if spec.type == "exec":
            return self._run_exec(spec.command, args)
        if spec.type == "alias":
            if _depth + 1 >= _MAX_ALIAS_DEPTH:
                raise QuickCommandError(
                    f"alias loop in /{name}: depth {_depth + 1} reached cap"
                )
            target = spec.target.lstrip("/")
            if not target:
                raise QuickCommandError(f"alias /{name} has no target")
            if self.dispatcher is None:
                raise QuickCommandError(
                    "no dispatcher wired for alias re-dispatch"
                )
            return self.dispatcher(target, args, _depth + 1)
        raise QuickCommandError(f"unknown quick-command type: {spec.type}")

    def _run_exec(self, command: str, args: str) -> QuickResult:
        full = f"{command} {args}".strip() if args else command
        try:
            cp = subprocess.run(
                full,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            out = (cp.stdout + cp.stderr)[:_MAX_OUTPUT]
            return QuickResult(output=out, timed_out=False)
        except subprocess.TimeoutExpired:
            return QuickResult(output="(timed out)", timed_out=True)
