"""Tools and plugin-preset setup section."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, checklist, radiolist
from opencomputer.plugins.recommended import RECOMMENDED_PLUGINS

_TOOLS = [
    Choice("🔍 Web Search & Scraping", "web_search"),
    Choice("🌐 Browser Automation", "browser"),
    Choice("💻 Terminal & Processes", "terminal"),
    Choice("📁 File Operations", "files"),
    Choice("⚙ Code Execution", "code"),
    Choice("👁 Vision / Image Analysis", "vision"),
    Choice("🎨 Image Generation", "image_generation"),
    Choice("🔊 Text-to-Speech", "tts"),
    Choice("🧩 Skills", "skills"),
    Choice("✅ Task Planning", "planning"),
    Choice("🧠 Memory", "memory"),
    Choice("🔎 Session Search", "session_search"),
    Choice("❓ Clarifying Questions", "clarify"),
    Choice("👥 Task Delegation", "delegation"),
    Choice("⏰ Cron Jobs", "cron"),
    Choice("💬 Cross-Platform Messaging", "messaging"),
    Choice("🖥 Computer Use", "computer_use"),
]

_DEFAULT_ENABLED = [
    "web_search",
    "browser",
    "terminal",
    "files",
    "code",
    "tts",
    "skills",
    "planning",
    "memory",
    "session_search",
    "clarify",
    "delegation",
    "cron",
    "messaging",
    "computer_use",
]

# Re-export the canonical tuple so existing imports of this name keep
# working; the single source of truth lives in `plugins.recommended`.
_RECOMMENDED_PLUGINS = RECOMMENDED_PLUGINS


def _apply_recommended_plugins(ctx: WizardCtx) -> None:
    plugins = ctx.config.setdefault("plugins", {})
    enabled = list(plugins.setdefault("enabled", []))
    for name in _RECOMMENDED_PLUGINS:
        if name not in enabled:
            enabled.append(name)
    plugins["enabled"] = enabled


def run_tools_section(ctx: WizardCtx) -> SectionResult:
    gate = [
        Choice("Configure recommended CLI tools", "configure"),
        Choice("Skip - keep current plugin set", "skip"),
    ]
    gate_idx = radiolist("Configure tools / plugins?", gate, default=0)
    if gate_idx == 1:
        return SectionResult.SKIPPED_FRESH

    pre_selected = [
        i for i, choice in enumerate(_TOOLS) if choice.value in _DEFAULT_ENABLED
    ]
    selected = checklist(
        "Tools for CLI",
        _TOOLS,
        pre_selected=pre_selected,
        show_markers=False,
    )
    ctx.config.setdefault("tools", {})["enabled"] = [
        str(_TOOLS[i].value) for i in selected
    ]
    _apply_recommended_plugins(ctx)
    print(f"  ✓ Enabled {len(selected)} tool categories")
    print("  ✓ Applied recommended plugin preset:")
    for name in _RECOMMENDED_PLUGINS:
        print(f"      • {name}")
    return SectionResult.CONFIGURED
