"""
Typed configuration — replaces the 58-parameter __init__ nightmare.

All agent config lives in small, composable dataclasses. Load from
~/.opencomputer/config.yaml (or TOML — TBD). Environment variables
can override individual fields.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    """Return ~/.opencomputer/, creating it if needed."""
    home = Path(os.environ.get("OPENCOMPUTER_HOME", Path.home() / ".opencomputer"))
    home.mkdir(parents=True, exist_ok=True)
    return home


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Which LLM to use and how.

    ``cheap_model``: When set, short/simple prompts (see
    ``agent/cheap_route.py``) are routed to this model instead of
    ``model`` for the first turn only. ``None`` = feature disabled.
    Example:
    ``ModelConfig(model="claude-sonnet-4-6", cheap_model="claude-haiku-4-5-20251001")``.
    """

    provider: str = "anthropic"  # maps to a provider plugin name
    model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    temperature: float = 1.0
    api_key_env: str = "ANTHROPIC_API_KEY"
    cheap_model: str | None = None
    # G.31 — smart model fallback routing. Ordered list of model ids to
    # try on transient errors (429 / 5xx / connection refused) when the
    # primary ``model`` fails. Empty tuple = no fallback (today's
    # behavior). Each fallback uses the same ``provider`` configured
    # above; cross-provider fallback is intentionally NOT supported here
    # to keep the failure mode predictable (mixing providers mid-turn
    # has subtle implications for tool schemas, streaming shape, prompt
    # cache identity).
    fallback_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Behavior of the main agent loop.

    ``delegation_max_iterations`` (II.1) is the independent iteration
    budget applied to subagent loops spawned via ``DelegateTool``.
    Mirrors Hermes's pattern (``sources/hermes-agent/run_agent.py``
    ``IterationBudget.__init__`` lines 185-196): parent gets its full
    ``max_iterations``, subagents get a tighter cap so runaway chains
    can't exhaust the parent's token budget.

    Two timeouts apply to a single ``run_conversation`` call (Round 2B P-3):

    * ``inactivity_timeout_s`` — wall-clock seconds since the last
      *activity* event (LLM request returning OR a tool dispatch
      finishing, success or failure). Resets every time the agent does
      something. Default 300s = 5 min. This is the timeout you usually
      want: a streaming bash that takes 20 minutes still proves the
      agent is alive every time another tool runs.
    * ``iteration_timeout_s`` — absolute wall-clock cap from when
      ``run_conversation`` is entered, regardless of activity.
      Default 1800s = 30 min. Prevents a pathological loop where the
      agent stays "active" forever (e.g. runs 1000 cheap tool calls).

    Both checks use ``time.monotonic()`` (clock-jump safe). Either
    timeout firing raises a typed exception (``InactivityTimeout`` /
    ``IterationTimeout``) — both subclass ``LoopTimeout`` so callers can
    catch one or the other.
    """

    max_iterations: int = 50
    parallel_tools: bool = True
    inactivity_timeout_s: int = 300
    iteration_timeout_s: int = 1800
    delegation_max_iterations: int = 50
    max_delegation_depth: int = 2
    """Cap on `DelegateTool` recursion. 2 = parent (depth 0) → child (depth 1) → grandchild (depth 2) rejected.
    Mirrors Hermes `MAX_DEPTH = 2` from `sources/hermes-agent/tools/delegate_tool.py`."""
    context_engine: str = "compressor"
    """Tier-A item 10 — which :class:`ContextEngine` strategy the loop uses.
    ``"compressor"`` is the default (existing CompactionEngine, aux-LLM
    summarization with safe boundary splitting). Plugins register
    alternatives via ``opencomputer.agent.context_engine_registry.register``
    — a profile setting other than ``"compressor"`` resolves through the
    registry; an unknown name logs a warning and falls back to the
    default so a misconfigured profile still boots."""


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Where sessions are stored and how."""

    db_path: Path = field(default_factory=lambda: _home() / "sessions.db")
    session_id: str | None = None  # None = create new session each run


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """The three-pillar memory configuration.

    Built-in memory is always on. An external provider (Honcho, Mem0,
    Cognee, etc.) is an overlay on top, controlled by `provider` field:
      - "memory-honcho"           → Honcho plugin (DEFAULT — requires Docker;
                                    setup wizard falls back to built-in
                                    cleanly if Docker is absent)
      - ""                        → built-in only (legacy; wizard may set
                                    this when Docker is detected absent)
      - "memory-mem0" / "memory-cognee" → future
    """

    declarative_path: Path = field(default_factory=lambda: _home() / "MEMORY.md")
    user_path: Path = field(default_factory=lambda: _home() / "USER.md")
    skills_path: Path = field(default_factory=lambda: _home() / "skills")
    #: Phase 14.F / C3 — per-profile personality file. Rendered into the
    #: FROZEN base prompt so ``SOUL.md`` drift (next session) picks up the
    #: new identity, but mid-session edits preserve prefix-cache hits.
    soul_path: Path = field(default_factory=lambda: _home() / "SOUL.md")
    # episodic memory uses SessionConfig.db_path

    enabled: bool = True
    memory_char_limit: int = 4000  # MEMORY.md cap injected into base prompt
    user_char_limit: int = 2000  # USER.md cap injected into base prompt
    # Phase 12b1 Sub-project A: Honcho is the default overlay when Docker
    # is available. Wizard writes "" back to config.yaml if Docker is
    # absent so subsequent runs don't keep trying to spin up the stack.
    provider: str = "memory-honcho"
    fallback_to_builtin: bool = True  # non-negotiable; here for docs
    enable_ambient_blocks: bool = True
    """When True (default), MemoryBridge.collect_system_prompt_blocks queries
    all active providers and the prompt builder injects their content under
    '## Memory context'. Disable to opt out without unloading the providers.
    PR-6 of 2026-04-25 Hermes parity plan."""
    max_ambient_block_chars: int = 800
    """Per-provider cap on system_prompt_block return value. Stays well under
    typical Anthropic prompt cache breakpoints. Provider implementations
    SHOULD respect this; bridge truncates if they don't.
    PR-6 of 2026-04-25 Hermes parity plan."""
    # Round 2A P-18 — episodic-memory dreaming. EXPERIMENTAL. OFF by default.
    # When enabled, an isolated lightweight LLM turn periodically clusters
    # recent episodic events and writes a per-cluster consolidation summary
    # back to episodic_events. Manual trigger: `opencomputer memory dream-now`.
    # Auto trigger: `opencomputer memory dream-on --interval daily|hourly` —
    # consult docs/memory_dreaming.md before promoting to default.
    dreaming_enabled: bool = False
    """When True, ``opencomputer memory dream-now`` (and any future
    scheduler) consolidates recent episodic-memory entries into per-cluster
    summaries. Originals stay readable but get tagged with a
    ``dreamed_into`` link to the consolidation row, so re-runs only process
    NEW entries."""
    dreaming_interval: str = "daily"
    """Cadence hint for ``opencomputer memory dream-on``. One of
    ``"daily"`` or ``"hourly"``. The CLI persists the chosen interval into
    config.yaml; today's CLI does not start a background scheduler — users
    drive consolidation via cron/launchd/systemd or by running
    ``dream-now`` manually."""


@dataclass(frozen=True, slots=True)
class HookCommandConfig:
    """One shell-command hook entry declared in config.yaml.

    III.6 — mirrors Claude Code's settings-format hook block
    (``sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md``).

    Users declare these under the top-level ``hooks:`` key in
    ``~/.opencomputer/<profile>/config.yaml`` and they're converted into
    ``HookSpec`` instances at CLI startup. Plugin-declared hooks and
    settings-declared hooks coexist; both fire for matching events.

    Attributes:
        event: Hook event name (``"PreToolUse"``, ``"PostToolUse"``,
            ``"Stop"``, etc.). Must match a :class:`HookEvent` enum value.
        command: Shell command to run. Env-var substitution happens via
            ``shlex.split`` at invocation time — do not rely on shell-only
            features like pipes/redirects inside a single command.
        matcher: Optional regex over tool name (PreToolUse / PostToolUse only).
        timeout_seconds: Hard wall-clock limit. Exceeded → hook is killed
            and the handler returns ``decision="pass"`` (fail-open).
    """

    event: str = ""  # "PreToolUse", "PostToolUse", "Stop", etc.
    command: str = ""  # shell command to run (env-var substitution allowed)
    matcher: str | None = None  # regex over tool name (PreToolUse / PostToolUse only)
    timeout_seconds: float = 10.0
    # "type": only "command" is supported (no LLM-prompt hooks yet)


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One MCP server the agent should connect to.

    Three transports are supported:
    - "stdio"   — local subprocess with command + args + env
    - "sse"     — legacy MCP HTTP transport (Server-Sent Events)
    - "http"    — modern MCP transport (Streamable HTTP, spec rev 2025-03+)
    """

    name: str = ""
    transport: str = "stdio"  # "stdio" | "sse" | "http"
    command: str = ""  # for stdio: the executable (e.g. "python3")
    args: tuple[str, ...] = ()  # for stdio: argv (use tuple for hashability)
    url: str = ""  # for sse/http: endpoint URL
    env: dict[str, str] = field(default_factory=dict)  # for stdio: env vars
    headers: dict[str, str] = field(default_factory=dict)  # for sse/http: HTTP headers (auth)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class MCPConfig:
    """MCP integration — list of servers + global toggles.

    OSV scanning (Round 2B P-7)
    ---------------------------
    Before spawning a stdio MCP server via ``npx``/``uvx``, the
    launcher consults the public OSV.dev advisory database for the
    package. Hits are emitted on the F2 bus as ``mcp_security.osv_hit``
    events for audit subscribers.

    * ``osv_check_enabled`` — master switch. ``True`` (default) runs
      the pre-flight check; ``False`` skips it entirely (no network,
      no cache touches).
    * ``osv_check_fail_closed`` — when ``True``, a HIGH-severity hit
      causes the launcher to refuse the spawn (raises). Default
      ``False`` keeps the warn-and-allow posture so an OSV outage
      can't break MCP startup.
    """

    servers: tuple[MCPServerConfig, ...] = ()
    # Connect servers in the background after startup (kimi-cli pattern).
    deferred: bool = True
    osv_check_enabled: bool = True
    osv_check_fail_closed: bool = False


@dataclass(frozen=True, slots=True)
class WebSearchConfig:
    """Per-tool config for the WebSearch tool (Phase 12d.2).

    Picks ONE provider per query. Multi-provider auto-fallback is
    intentionally not built in — predictable cost, predictable rate-limit
    blast radius. Add an explicit `--fallback` flag in a later PR if
    the dogfood log says it's needed.

    API keys live in env vars (BRAVE_API_KEY, TAVILY_API_KEY, etc.) —
    NOT in this YAML. Writing tokens to config.yaml is the kind of
    decision that gets a key committed to git six months from now.
    """

    #: One of "ddg" | "brave" | "tavily" | "exa" | "firecrawl".
    #: DDG is the only keyless option and the safe default.
    provider: str = "ddg"


@dataclass(frozen=True, slots=True)
class ToolsConfig:
    """Per-tool configuration. Add a new field per tool that needs settings."""

    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)


@dataclass(frozen=True, slots=True)
class FullSystemControlConfig:
    """3.F — master enable/disable for autonomous full-system-control mode.

    Independent of F1 consent gating: F1 controls per-tool authorization;
    this controls the whole autonomous-mode personality. When disabled
    (default), the agent behaves like a standard chat agent.

    Attributes:
        enabled: Master switch. ``False`` = invisible / standard chat
            agent. ``True`` = autonomous-mode personality engaged
            (structured agent log, optional menu-bar indicator).
        log_path: Where the structured JSON-line agent log is written.
            Defaults to ``~/.opencomputer/<profile>/agent.log`` via
            :func:`_home`.
        menu_bar_indicator: macOS-only best-effort indicator. Activated
            via the ``--menu-bar`` flag on ``opencomputer system-control
            enable``; soft-deps on the optional ``rumps`` extra. Stays
            ``False`` on Linux/Windows even if requested.
        json_log_max_size_bytes: When the log exceeds this size, the
            current file is renamed to ``<log_path>.old`` and a fresh
            file starts. One ``.old`` rolloff only — admins use
            ``logrotate`` for long retention.
    """

    enabled: bool = False
    log_path: Path = field(default_factory=lambda: _home() / "agent.log")
    menu_bar_indicator: bool = False  # macOS only; best-effort via rumps if installed
    json_log_max_size_bytes: int = 50 * 1024 * 1024  # 50 MB; rotate after


@dataclass(frozen=True, slots=True)
class DeepeningConfig:
    """Layer 3 deepening — content extractor + cost controls (2026-04-28).

    The extractor reads the *content* of recent files / browser pages /
    calendar events and runs an LLM over each to extract structured
    signals (topic, intent, people). Default backend is Ollama
    (privacy-by-default — content never leaves the machine). Users
    with an existing Anthropic/OpenAI key can switch via this config
    block to skip installing a second LLM stack.

    ``extractor`` is a free-form ``str`` rather than a closed Literal so
    adding a new backend (Gemini, llama-cpp) doesn't break the schema.
    The factory in :mod:`opencomputer.profile_bootstrap.llm_extractor`
    validates against the canonical list at runtime.
    """

    extractor: str = "ollama"
    """One of: "ollama" (default — local, private), "anthropic", "openai"."""

    model: str = ""
    """Model id passed to the extractor. Empty → backend default
    (llama3.2:3b / claude-haiku-4-5-20251001 / gpt-4o-mini)."""

    daily_cost_cap_usd: float = 0.50
    """Per-day spend ceiling. Cost guard skips further extractions on
    the same UTC day once exceeded. Ollama bypasses cost guard."""

    max_artifacts_per_pass: int = 100
    timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class Config:
    """Root configuration — composed of small focused configs."""

    model: ModelConfig = field(default_factory=ModelConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    #: 2026-04-28 — Layer 3 extractor + cost controls.
    deepening: DeepeningConfig = field(default_factory=DeepeningConfig)
    #: III.6 — settings-declared shell-command hooks. Parsed from the
    #: top-level ``hooks:`` YAML block by
    #: :func:`opencomputer.agent.config_store._parse_hooks_block` and
    #: registered into the global :class:`HookEngine` at CLI startup.
    hooks: tuple[HookCommandConfig, ...] = ()
    #: 3.F — master enable/disable for autonomous full-system-control mode.
    #: Defaults to disabled (invisible). When enabled, the structured
    #: ``agent.log`` collector + optional menu-bar indicator activate.
    system_control: FullSystemControlConfig = field(default_factory=FullSystemControlConfig)
    home: Path = field(default_factory=_home)


def default_config() -> Config:
    """Return the default configuration with filesystem-appropriate paths."""
    return Config()


__all__ = [
    "Config",
    "ModelConfig",
    "LoopConfig",
    "SessionConfig",
    "MemoryConfig",
    "DeepeningConfig",
    "MCPConfig",
    "MCPServerConfig",
    "HookCommandConfig",
    "ToolsConfig",
    "WebSearchConfig",
    "FullSystemControlConfig",
    "default_config",
]
