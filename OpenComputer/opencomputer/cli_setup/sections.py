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
    from opencomputer.cli_setup.section_handlers.agent_settings import (
        is_agent_settings_configured,
        run_agent_settings_section,
    )
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
        run_inference_provider_section,
    )
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        is_messaging_platforms_configured,
        run_messaging_platforms_section,
    )
    from opencomputer.cli_setup.section_handlers.prior_install import (
        run_prior_install_section,
    )
    from opencomputer.cli_setup.section_handlers.service_install import (
        run_service_install_section,
    )
    from opencomputer.cli_setup.section_handlers.terminal_backend import (
        run_terminal_backend_section,
    )
    from opencomputer.cli_setup.section_handlers.tools import (
        run_tools_section,
    )
    from opencomputer.cli_setup.section_handlers.tts_provider import (
        run_tts_provider_section,
    )

    return [
        WizardSection(
            key="opencomputer_prior_detect", icon="◆",
            title="OpenClaw / Hermes Installation Detected",
            description=(
                "Detect ~/.openclaw or ~/.hermes data and offer to import "
                "MEMORY/USER/SOUL files + skills/ (non-destructive)."
            ),
            handler=run_prior_install_section,
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
            key="agent_settings", icon="◆", title="Agent Settings",
            description=(
                "Max iterations, parallel tools, inactivity + iteration "
                "timeouts.\n"
                "Recommended defaults work for most use cases."
            ),
            handler=run_agent_settings_section,
            configured_check=is_agent_settings_configured,
        ),
        WizardSection(
            key="tts_provider", icon="◆",
            title="Text-to-Speech Provider (optional)",
            description=(
                "Voice output. Default: Edge TTS (free, no API key). "
                "Premium engines (ElevenLabs, OpenAI, xAI, NeuTTS, KittenTTS) "
                "configurable later via config.yaml."
            ),
            handler=run_tts_provider_section,
        ),
        WizardSection(
            key="terminal_backend", icon="◆", title="Terminal Backend",
            description=(
                "Where Hermes runs shell commands and code. "
                "Choose: local (default) / docker / apptainer."
            ),
            handler=run_terminal_backend_section,
        ),
        WizardSection(
            key="tools", icon="◆", title="Tools",
            description=(
                "Enable the recommended plugin preset "
                "(coding-harness + memory-honcho + dev-tools)."
            ),
            handler=run_tools_section,
        ),
        WizardSection(
            key="service_install", icon="◆", title="Always-On System Service",
            description=(
                "Install the gateway as a system service so it runs on login\n"
                "and restarts on crash. Cross-platform: launchd (macOS),\n"
                "systemd-user (Linux), Task Scheduler (Windows)."
            ),
            handler=run_service_install_section,
        ),
    ]


SECTION_REGISTRY: list[WizardSection] = _build_registry()
