"""Data model for the wizard's section-driven flow.

Visual + UX modeled after hermes-agent's hermes_cli/setup.py::run_setup_wizard.
Independently re-implemented (no code copied) — see spec § 10 O1.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SectionResult(Enum):
    CONFIGURED = "configured"
    SKIPPED_KEEP = "skipped-keep"
    SKIPPED_FRESH = "skipped-fresh"
    CANCELLED = "cancelled"


@dataclass
class WizardCtx:
    """Threaded through every section handler. Mutating ``config`` is
    expected; the orchestrator persists the dict to disk after all
    sections run."""

    config: dict
    config_path: Path
    is_first_run: bool
    quick_mode: bool = False
    extra: dict = field(default_factory=dict)


HandlerFn = Callable[["WizardCtx"], "SectionResult"]
ConfiguredCheckFn = Callable[["WizardCtx"], bool]


@dataclass
class WizardSection:
    """One step in the wizard. Handlers and configured_check both
    receive the WizardCtx."""

    key: str
    icon: str
    title: str
    description: str
    handler: HandlerFn
    configured_check: ConfiguredCheckFn | None = None
    deferred: bool = False
    target_subproject: str = ""


def _build_registry() -> list[WizardSection]:
    """Single source of truth for section order. Imports happen here
    (not at module top) so deferred-section subprojects can register
    without circular imports."""
    from opencomputer.cli_setup.section_handlers._deferred import (
        make_deferred_handler,
    )
    from opencomputer.cli_setup.section_handlers.agent_settings import (
        is_agent_settings_configured,
        run_agent_settings_section,
    )
    from opencomputer.cli_setup.section_handlers.launchd_service import (
        run_launchd_service_section,
    )
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
        run_inference_provider_section,
    )
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        is_messaging_platforms_configured,
        run_messaging_platforms_section,
    )

    return [
        WizardSection(
            key="opencomputer_prior_detect", icon="◆",
            title="Prior install detection",
            description="Detect existing OpenClaw / Hermes / OpenComputer data and offer to migrate.",
            handler=make_deferred_handler("M1"), deferred=True, target_subproject="M1",
        ),
        WizardSection(
            key="inference_provider", icon="◆",
            title="Inference Provider",
            description=(
                "Choose how to connect to your main chat model.\n"
                "Guide: https://github.com/sakshamzip2-sys/opencomputer#providers"
            ),
            handler=run_inference_provider_section,
            configured_check=is_inference_provider_configured,
        ),
        WizardSection(
            key="messaging_platforms", icon="◆",
            title="Messaging Platforms",
            description=(
                "Connect to messaging platforms to chat with OpenComputer from anywhere.\n"
                "Toggle with Space, confirm with Enter."
            ),
            handler=run_messaging_platforms_section,
            configured_check=is_messaging_platforms_configured,
        ),
        WizardSection(
            key="agent_settings", icon="◆", title="Agent settings",
            description=(
                "Max iterations, parallel tools, inactivity + iteration "
                "timeouts.\n"
                "Recommended defaults work for most use cases."
            ),
            handler=run_agent_settings_section,
            configured_check=is_agent_settings_configured,
        ),
        WizardSection(
            key="tts_provider", icon="◆", title="TTS provider",
            description="Voice output: NeutTTS / KittenTTS / eSpeak-NG / ElevenLabs / OpenAI TTS.",
            handler=make_deferred_handler("S2"), deferred=True, target_subproject="S2",
        ),
        WizardSection(
            key="terminal_backend", icon="◆", title="Terminal backend",
            description="Sandboxed shell: Apptainer / Docker / native.",
            handler=make_deferred_handler("S3"), deferred=True, target_subproject="S3",
        ),
        WizardSection(
            key="tools", icon="◆", title="Tools",
            description="Optional tool plugins.",
            handler=make_deferred_handler("S4"), deferred=True, target_subproject="S4",
        ),
        WizardSection(
            key="launchd_service", icon="◆", title="Launchd service",
            description=(
                "Run gateway as a launchd service (starts on login).\n"
                "macOS only — no-op on Linux/Windows."
            ),
            handler=run_launchd_service_section,
        ),
    ]


SECTION_REGISTRY: list[WizardSection] = _build_registry()
