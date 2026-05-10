"""Data model and registry for the section-driven setup wizard."""
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
    """Shared mutable context passed through all setup sections."""

    config: dict
    config_path: Path
    is_first_run: bool
    quick_mode: bool = False
    extra: dict = field(default_factory=dict)


HandlerFn = Callable[[WizardCtx], SectionResult]
ConfiguredCheckFn = Callable[[WizardCtx], bool]


@dataclass
class WizardSection:
    """One setup section."""

    key: str
    icon: str
    title: str
    description: str
    handler: HandlerFn
    configured_check: ConfiguredCheckFn | None = None
    deferred: bool = False
    target_subproject: str = ""
    quick: bool = False


def _build_registry() -> list[WizardSection]:
    from opencomputer.cli_setup.section_handlers.agent_settings import (
        is_agent_settings_configured,
        run_agent_settings_section,
    )
    from opencomputer.cli_setup.section_handlers.configuration_location import (
        run_configuration_location_section,
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
    from opencomputer.cli_setup.section_handlers.terminal_backend import (
        run_terminal_backend_section,
    )
    from opencomputer.cli_setup.section_handlers.tools import run_tools_section
    from opencomputer.cli_setup.section_handlers.tts_provider import (
        run_tts_provider_section,
    )
    from opencomputer.cli_setup.section_handlers.vision_provider import (
        is_vision_provider_configured,
        run_vision_provider_section,
    )

    return [
        WizardSection(
            key="configuration_location",
            icon="◆",
            title="Configuration Location",
            description=(
                "OpenComputer stores settings, secrets, and data under "
                "~/.opencomputer."
            ),
            handler=run_configuration_location_section,
            quick=True,
        ),
        WizardSection(
            key="opencomputer_prior_detect",
            icon="◆",
            title="OpenClaw / Hermes Installation Detected",
            description=(
                "Detect ~/.openclaw or ~/.hermes data and offer to import "
                "MEMORY/USER/SOUL files + skills/ non-destructively."
            ),
            handler=run_prior_install_section,
        ),
        WizardSection(
            key="inference_provider",
            icon="◆",
            title="Inference Provider",
            description=(
                "Choose how to connect to your main chat model.\n"
                "Guide: https://github.com/sakshamzip2-sys/opencomputer#providers"
            ),
            handler=run_inference_provider_section,
            configured_check=is_inference_provider_configured,
            quick=True,
        ),
        WizardSection(
            key="vision_provider",
            icon="◆",
            title="Vision & Image Analysis (optional)",
            description=(
                "Vision uses a separate multimodal backend. Skip now if "
                "you do not need image analysis yet."
            ),
            handler=run_vision_provider_section,
            configured_check=is_vision_provider_configured,
        ),
        WizardSection(
            key="tts_provider",
            icon="◆",
            title="Text-to-Speech Provider (optional)",
            description="Choose a voice output backend or keep it disabled.",
            handler=run_tts_provider_section,
        ),
        WizardSection(
            key="terminal_backend",
            icon="◆",
            title="Terminal Backend",
            description=(
                "Choose where OpenComputer runs shell commands and code. "
                "This affects execution, file access, and isolation."
            ),
            handler=run_terminal_backend_section,
            quick=True,
        ),
        WizardSection(
            key="agent_settings",
            icon="◆",
            title="Agent Settings",
            description=(
                "Max iterations, tool progress display, context compression, "
                "and reset policy."
            ),
            handler=run_agent_settings_section,
            configured_check=is_agent_settings_configured,
            quick=True,
        ),
        WizardSection(
            key="messaging_platforms",
            icon="◆",
            title="Messaging Platforms",
            description=(
                "Connect Telegram, Discord, Slack, Matrix, and other channel "
                "plugins."
            ),
            handler=run_messaging_platforms_section,
            configured_check=is_messaging_platforms_configured,
            quick=True,
        ),
        WizardSection(
            key="tools",
            icon="◆",
            title="Tools / Tool Providers",
            description=(
                "Enable the recommended CLI tool set and provider-specific "
                "tool integrations."
            ),
            handler=run_tools_section,
        ),
    ]


SECTION_REGISTRY: list[WizardSection] = _build_registry()
