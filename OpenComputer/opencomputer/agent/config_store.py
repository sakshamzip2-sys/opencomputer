"""
Config persistence — load/save ~/.opencomputer/config.yaml.

Users can edit this file by hand or via `opencomputer config set key=value`.
Defaults from ModelConfig/LoopConfig/etc. apply if the file is missing
or if a given key isn't set.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from opencomputer.agent.config import (
    Config,
    HookCommandConfig,
    _home,
    default_config,
)

_log = logging.getLogger("opencomputer.config")


def config_file_path() -> Path:
    return _home() / "config.yaml"


# ─── load ────────────────────────────────────────────────────────


def _apply_overrides(base: Any, overrides: dict[str, Any]) -> Any:
    """Recursively apply a dict over a dataclass, returning a new dataclass."""
    if not is_dataclass(base) or not isinstance(overrides, dict):
        return base
    field_map = {f.name: f for f in fields(base)}
    kwargs: dict[str, Any] = {}
    for name, current in asdict(base).items():
        if name in overrides:
            new = overrides[name]
            nested = getattr(base, name)

            if is_dataclass(nested) and isinstance(new, dict):
                # Nested dataclass (e.g. model, loop, mcp)
                kwargs[name] = _apply_overrides(nested, new)
            elif isinstance(nested, tuple) and isinstance(new, list):
                # Tuple-of-dataclasses field (e.g. mcp.servers = [MCPServerConfig, ...])
                inner_type = _extract_tuple_inner_type(type(base), name, nested)
                if inner_type is not None:
                    built = []
                    for item in new:
                        if isinstance(item, dict):
                            # build a default instance then apply overrides
                            try:
                                default_instance = inner_type()
                            except TypeError:
                                default_instance = None
                            if default_instance is not None:
                                built.append(_apply_overrides(default_instance, item))
                            else:
                                built.append(item)
                        else:
                            built.append(item)
                    kwargs[name] = tuple(built)
                else:
                    kwargs[name] = tuple(new)
            else:
                field_type = field_map[name].type
                if "Path" in str(field_type) and isinstance(new, str):
                    kwargs[name] = Path(new)
                else:
                    kwargs[name] = new
        else:
            kwargs[name] = getattr(base, name)
    return type(base)(**kwargs)


def _extract_tuple_inner_type(
    base_cls: type, field_name: str, existing_tuple: tuple
) -> type | None:
    """Best-effort: figure out the dataclass type stored in a tuple field.

    Uses typing.get_type_hints so 'from __future__ import annotations'
    string annotations are resolved to real types.
    """
    if existing_tuple and is_dataclass(existing_tuple[0]):
        return type(existing_tuple[0])
    import typing

    try:
        hints = typing.get_type_hints(base_cls)
    except Exception:
        return None
    annotation = hints.get(field_name)
    if annotation is None:
        return None
    origin = typing.get_origin(annotation)
    if origin is tuple:
        args = typing.get_args(annotation)
        if args and is_dataclass(args[0]):
            return args[0]
    return None


def _parse_hooks_block(block: Any) -> tuple[HookCommandConfig, ...]:
    """Convert the top-level ``hooks:`` YAML block into a flat
    tuple of :class:`HookCommandConfig`.

    III.6 — mirrors Claude Code's settings-format hook declarations
    (``sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md``).

    Accepts two shapes:

    **Nested (event-keyed)** — matches Claude Code's settings.json layout::

        hooks:
          PreToolUse:
            - matcher: "Edit|Write|MultiEdit"
              command: "python3 /path/to/linter.py"
              timeout_seconds: 10
          Stop:
            - command: "bash /path/to/cleanup.sh"

    **Flat list** — friendlier for programmatic config generation::

        hooks:
          - event: PreToolUse
            matcher: "Edit|Write"
            command: "..."
          - event: Stop
            command: "..."

    Malformed entries (missing ``command``, unknown event name, wrong types)
    are logged at WARNING and skipped so one bad hook can't break startup.
    ``None`` / missing block → empty tuple.
    """
    from plugin_sdk.hooks import HookEvent

    if block is None:
        return ()
    valid_events = {e.value for e in HookEvent}

    def _coerce(raw: dict, default_event: str | None = None) -> HookCommandConfig | None:
        # Support both "type": "command" and no-type-field entries.
        # "type": "prompt" (M8.1) and "type": "agent" (M8.2) are valid
        # types parsed by sibling helpers — silently skip them here so
        # the same YAML block can mix command, prompt, and agent hooks.
        hook_type = raw.get("type", "command")
        if hook_type in ("prompt", "agent"):
            return None
        if hook_type != "command":
            _log.warning(
                "hooks: skipping entry with unsupported type %r (expected 'command', 'prompt', or 'agent'): %r",
                hook_type,
                raw,
            )
            return None
        event_name = raw.get("event", default_event)
        if not isinstance(event_name, str) or not event_name:
            _log.warning("hooks: skipping entry missing event name: %r", raw)
            return None
        if event_name not in valid_events:
            _log.warning(
                "hooks: skipping entry with unknown event %r (expected one of %s)",
                event_name,
                sorted(valid_events),
            )
            return None
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            _log.warning("hooks: skipping entry missing command: %r", raw)
            return None
        matcher_value = raw.get("matcher")
        if matcher_value is not None and not isinstance(matcher_value, str):
            _log.warning("hooks: skipping entry with non-string matcher: %r", raw)
            return None
        timeout_value = raw.get("timeout_seconds", raw.get("timeout", 10.0))
        try:
            timeout_seconds = float(timeout_value)
        except (TypeError, ValueError):
            _log.warning(
                "hooks: skipping entry with non-numeric timeout_seconds=%r: %r",
                timeout_value,
                raw,
            )
            return None
        return HookCommandConfig(
            event=event_name,
            command=command,
            matcher=matcher_value,
            timeout_seconds=timeout_seconds,
        )

    parsed: list[HookCommandConfig] = []

    if isinstance(block, list):
        # Flat list — each entry must carry its own "event" field.
        for entry in block:
            if not isinstance(entry, dict):
                _log.warning("hooks: skipping non-mapping list entry: %r", entry)
                continue
            spec = _coerce(entry)
            if spec is not None:
                parsed.append(spec)
        return tuple(parsed)

    if isinstance(block, dict):
        # Nested: { "PreToolUse": [ {matcher, command, ...}, ... ], ... }
        for event_name, entries in block.items():
            if not isinstance(entries, list):
                _log.warning(
                    "hooks: skipping %r — expected list of entries, got %s",
                    event_name,
                    type(entries).__name__,
                )
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    _log.warning(
                        "hooks: skipping non-mapping entry under %r: %r",
                        event_name,
                        entry,
                    )
                    continue
                spec = _coerce(entry, default_event=event_name)
                if spec is not None:
                    parsed.append(spec)
        return tuple(parsed)

    _log.warning(
        "hooks: top-level block must be a mapping or a list, got %s; ignoring",
        type(block).__name__,
    )
    return ()


class ConfigYAMLError(Exception):
    """Raised when a YAML config file cannot be parsed or has the wrong shape.

    Centralizes the failure surface for :func:`load_yaml_dict`. Carries
    the offending ``path`` and a short cause string so CLI surfaces can
    render a user-facing message without re-formatting an exception.
    """

    def __init__(self, path: Path, cause: str | Exception) -> None:
        self.path = path
        self.cause = str(cause)
        super().__init__(f"{path}: {self.cause}")


def load_yaml_dict(
    path: Path,
    *,
    encoding: str = "utf-8",
    missing_ok: bool = True,
) -> dict[str, Any]:
    """Read a YAML file and return its top-level mapping.

    M1.2 — single canonical YAML→dict loader for profile.yaml /
    config.yaml. Replaces the scattered
    ``yaml.safe_load(path.read_text()) or {}`` boilerplate so error
    paths (missing file, parse failure, non-mapping top level) behave
    the same everywhere a config file is read.

    Behavior:
        * ``missing_ok=True`` (default) and the file does not exist →
          returns ``{}``. Mirrors the prior implicit behavior of
          ``or {}`` after a missing-file ``not exists()`` check.
        * ``missing_ok=False`` and the file does not exist → raises
          :class:`FileNotFoundError`.
        * Empty file → returns ``{}``.
        * Parse failure → raises :class:`ConfigYAMLError` (chained from
          ``yaml.YAMLError``).
        * Top-level value is not a mapping (list, scalar, etc.) →
          raises :class:`ConfigYAMLError`.

    Callers add their own schema validation on top of the returned
    dict. This helper deliberately does NOT validate keys, since
    profile.yaml has both a strict consumer (``load_config``) and
    lenient consumers (the ``oc plugin enable`` / ``oc profile
    env-template`` paths) that share one parse but differ on what
    they accept.
    """
    if not path.exists():
        if missing_ok:
            return {}
        raise FileNotFoundError(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding=encoding)) or {}
    except yaml.YAMLError as exc:
        raise ConfigYAMLError(path, exc) from exc
    if not isinstance(raw, dict):
        raise ConfigYAMLError(
            path,
            f"top-level YAML must be a mapping, got {type(raw).__name__}",
        )
    return raw


def _parse_prompt_hooks_block(block: Any) -> tuple[Any, ...]:
    """Convert the top-level ``hooks:`` YAML block into a flat tuple of
    :class:`HookPromptConfig`.

    v1.1 plan-2 M8.1 (2026-05-09). Scans the same YAML block as
    :func:`_parse_hooks_block` but only consumes entries with
    ``type: prompt``. Entries without a ``type`` field, or with
    ``type: command``, are silently skipped (the command parser
    picks them up).

    The frontmatter shape (per-entry) is::

        - type: prompt
          matcher: "Bash"          # optional regex over tool name
          system: |                # required — the policy prompt
            Reply 'block' if dangerous, 'allow' otherwise.
          model: auto              # optional — auto = cheap-route default
          returns: allow_block     # optional — allow_block | score
          timeout_seconds: 5
          token_budget_input: 500
          token_budget_output: 100
          score_threshold: 7.0     # only used when returns=score

    Malformed entries (missing ``system`` body, unknown event, wrong
    types) are logged at WARNING and skipped — one bad hook can't
    brick the CLI.
    """
    from opencomputer.agent.config import HookPromptConfig
    from plugin_sdk.hooks import HookEvent

    if block is None:
        return ()
    valid_events = {e.value for e in HookEvent}
    valid_returns = {"allow_block", "score"}

    def _coerce(raw: dict, default_event: str | None = None):
        if raw.get("type") != "prompt":
            return None
        event_name = raw.get("event", default_event)
        if not isinstance(event_name, str) or not event_name:
            _log.warning("prompt hooks: skipping entry missing event name: %r", raw)
            return None
        if event_name not in valid_events:
            _log.warning(
                "prompt hooks: skipping entry with unknown event %r (expected one of %s)",
                event_name, sorted(valid_events),
            )
            return None
        system = raw.get("system")
        if not isinstance(system, str) or not system.strip():
            _log.warning(
                "prompt hooks: skipping entry missing 'system' body: %r", raw,
            )
            return None
        matcher_value = raw.get("matcher")
        if matcher_value is not None and not isinstance(matcher_value, str):
            _log.warning(
                "prompt hooks: skipping entry with non-string matcher: %r", raw,
            )
            return None
        model = raw.get("model", "auto")
        if not isinstance(model, str) or not model.strip():
            model = "auto"
        returns = raw.get("returns", "allow_block")
        if returns not in valid_returns:
            _log.warning(
                "prompt hooks: skipping entry with unknown returns=%r "
                "(expected %s)",
                returns, sorted(valid_returns),
            )
            return None
        try:
            timeout_seconds = float(raw.get("timeout_seconds", 5.0))
        except (TypeError, ValueError):
            _log.warning(
                "prompt hooks: skipping entry with non-numeric timeout: %r", raw,
            )
            return None
        try:
            token_budget_input = int(raw.get("token_budget_input", 500))
            token_budget_output = int(raw.get("token_budget_output", 100))
        except (TypeError, ValueError):
            _log.warning(
                "prompt hooks: skipping entry with non-integer token budgets: %r",
                raw,
            )
            return None
        try:
            score_threshold = float(raw.get("score_threshold", 7.0))
        except (TypeError, ValueError):
            _log.warning(
                "prompt hooks: skipping entry with non-numeric score_threshold: %r",
                raw,
            )
            return None
        return HookPromptConfig(
            event=event_name,
            system=system,
            model=model,
            returns=returns,
            matcher=matcher_value,
            timeout_seconds=timeout_seconds,
            token_budget_input=token_budget_input,
            token_budget_output=token_budget_output,
            score_threshold=score_threshold,
        )

    parsed: list = []

    if isinstance(block, list):
        for entry in block:
            if not isinstance(entry, dict):
                continue
            spec = _coerce(entry)
            if spec is not None:
                parsed.append(spec)
        return tuple(parsed)

    if isinstance(block, dict):
        for event_name, entries in block.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                spec = _coerce(entry, default_event=event_name)
                if spec is not None:
                    parsed.append(spec)
        return tuple(parsed)

    return ()


def _parse_agent_hooks_block(block: Any) -> tuple[Any, ...]:
    """Convert the top-level ``hooks:`` YAML block into a flat tuple of
    :class:`HookAgentConfig`.

    v1.1 plan-2 M8.2 (2026-05-09). Sibling to
    :func:`_parse_prompt_hooks_block`; consumes only entries with
    ``type: agent``. The same YAML block can carry ``type: command``,
    ``type: prompt``, and ``type: agent`` entries side-by-side; each
    parser claims the entries that match its discriminator and silently
    ignores the others.

    Frontmatter shape::

        - type: agent
          matcher: "Bash"
          prompt: |                  # required
            Inspect the command and reply 'block: <reason>' or 'allow'.
          agent: code-reviewer       # optional registered template
          isolation: copy            # none | worktree | copy (default copy)
          max_turns: 5
          timeout_seconds: 60
          returns: allow_block       # or 'structured'
          token_budget_total: 5000

    Malformed entries (missing ``prompt`` body, unknown event /
    isolation / returns) are logged at WARNING and skipped.
    """
    from opencomputer.agent.config import HookAgentConfig
    from plugin_sdk.hooks import HookEvent

    if block is None:
        return ()
    valid_events = {e.value for e in HookEvent}
    valid_isolations = {"none", "worktree", "copy"}
    valid_returns = {"allow_block", "structured"}

    def _coerce(raw: dict, default_event: str | None = None):
        if raw.get("type") != "agent":
            return None
        event_name = raw.get("event", default_event)
        if not isinstance(event_name, str) or not event_name:
            _log.warning("agent hooks: skipping entry missing event: %r", raw)
            return None
        if event_name not in valid_events:
            _log.warning(
                "agent hooks: skipping entry with unknown event %r (expected one of %s)",
                event_name, sorted(valid_events),
            )
            return None
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            _log.warning(
                "agent hooks: skipping entry missing 'prompt' body: %r", raw,
            )
            return None
        matcher_value = raw.get("matcher")
        if matcher_value is not None and not isinstance(matcher_value, str):
            _log.warning(
                "agent hooks: skipping entry with non-string matcher: %r", raw,
            )
            return None
        agent = raw.get("agent", "")
        if not isinstance(agent, str):
            _log.warning(
                "agent hooks: skipping entry with non-string agent: %r", raw,
            )
            return None
        isolation = raw.get("isolation", "copy")
        if isolation not in valid_isolations:
            _log.warning(
                "agent hooks: skipping entry with unknown isolation=%r "
                "(expected one of %s)",
                isolation, sorted(valid_isolations),
            )
            return None
        returns = raw.get("returns", "allow_block")
        if returns not in valid_returns:
            _log.warning(
                "agent hooks: skipping entry with unknown returns=%r "
                "(expected one of %s)",
                returns, sorted(valid_returns),
            )
            return None
        try:
            max_turns = int(raw.get("max_turns", 5))
            timeout_seconds = float(raw.get("timeout_seconds", 60.0))
            token_budget_total = int(raw.get("token_budget_total", 5000))
        except (TypeError, ValueError):
            _log.warning(
                "agent hooks: skipping entry with non-numeric budgets: %r", raw,
            )
            return None
        if max_turns < 1:
            _log.warning(
                "agent hooks: skipping entry with non-positive max_turns: %r", raw,
            )
            return None
        return HookAgentConfig(
            event=event_name,
            prompt=prompt,
            agent=agent.strip(),
            isolation=isolation,
            returns=returns,
            matcher=matcher_value,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            token_budget_total=token_budget_total,
        )

    parsed: list = []
    if isinstance(block, list):
        for entry in block:
            if not isinstance(entry, dict):
                continue
            spec = _coerce(entry)
            if spec is not None:
                parsed.append(spec)
        return tuple(parsed)

    if isinstance(block, dict):
        for event_name, entries in block.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                spec = _coerce(entry, default_event=event_name)
                if spec is not None:
                    parsed.append(spec)
        return tuple(parsed)
    return ()


def _normalize_mcp_server_dict(raw: dict) -> dict:
    """Convert Hermes-spec nested MCP-server YAML to flat ``MCPServerConfig`` fields.

    Hermes spec form::

        mcp_servers:
          github:
            tools:
              include: [create_issue, list_issues]
              prompts: false
              resources: false

    Maps to OC dataclass fields ``tools_allow``, ``tools_deny``,
    ``prompts_enabled``, ``resources_enabled``. The flat OC-native form
    is left unchanged.

    G9 (Hermes parity, 2026-05-09).
    """
    out = dict(raw)
    tools = out.pop("tools", None)
    if isinstance(tools, dict):
        if "include" in tools:
            out["tools_allow"] = list(tools["include"])
        if "exclude" in tools:
            out["tools_deny"] = list(tools["exclude"])
        if "prompts" in tools:
            out["prompts_enabled"] = bool(tools["prompts"])
        if "resources" in tools:
            out["resources_enabled"] = bool(tools["resources"])
    elif tools is not None:
        # Non-dict (e.g. a stray list) — restore so the caller sees the
        # original shape and can complain about it.
        out["tools"] = tools
    return out


def _parse_routing_block(block: Any) -> Any:  # RoutingConfig | None at runtime
    """Convert the top-level ``routing:`` YAML block into :class:`RoutingConfig`.

    v1.1 plan-3 M10.1 (2026-05-09). Shape::

        routing:
          rules:
            - match: {platform: slack, channel: "#security-alerts"}
              agent: security-reviewer
            - match: {platform: telegram, peer: "<chat_id>"}
              agent: executive-assistant
              profile: executive
          default:
            agent: default

    Returns ``None`` when ``block`` is empty / missing — caller leaves
    the default :attr:`Config.routing` untouched. Returns
    :class:`RoutingConfig` on success. Malformed entries (rule missing
    ``agent``) are logged at WARNING and skipped.
    """
    from opencomputer.agent.config import (
        RoutingConfig,
        RoutingDefault,
        RoutingMatch,
        RoutingRule,
    )
    from opencomputer.agent.routing import sort_rules_by_specificity

    if not block:
        return None
    if not isinstance(block, dict):
        _log.warning("routing: top-level block must be a mapping; got %r", type(block))
        return None

    parsed_rules: list[RoutingRule] = []
    rules_raw = block.get("rules")
    if rules_raw is None:
        rules_iter = []
    elif isinstance(rules_raw, list):
        rules_iter = rules_raw
    else:
        _log.warning(
            "routing.rules: expected a list, got %r — ignoring", type(rules_raw)
        )
        rules_iter = []

    valid_match_keys = {
        "platform", "chat_id", "channel", "guild", "team",
        "account", "role", "peer",
    }

    for raw_rule in rules_iter:
        if not isinstance(raw_rule, dict):
            _log.warning("routing.rules: skipping non-dict entry %r", raw_rule)
            continue
        agent = raw_rule.get("agent")
        if not isinstance(agent, str) or not agent:
            _log.warning(
                "routing.rules: skipping entry missing required `agent`: %r",
                raw_rule,
            )
            continue
        match_dict = raw_rule.get("match", {}) or {}
        if not isinstance(match_dict, dict):
            _log.warning(
                "routing.rules: `match` must be a mapping; got %r — skipping",
                type(match_dict),
            )
            continue
        # Drop unknown match dimensions with a warning rather than a hard
        # error — keeps forward-compat for OpenClaw fields we haven't
        # ported yet (and keeps a typo from bricking the whole config).
        clean_match = {}
        for k, v in match_dict.items():
            if k not in valid_match_keys:
                _log.warning(
                    "routing.rules: unknown match dimension %r in rule %r — ignored",
                    k, raw_rule,
                )
                continue
            clean_match[k] = str(v) if v is not None else ""
        try:
            match_obj = RoutingMatch(**clean_match)
        except TypeError as e:
            _log.warning("routing.rules: invalid match block %r: %s", match_dict, e)
            continue
        try:
            rule_obj = RoutingRule(
                match=match_obj,
                agent=agent,
                profile=str(raw_rule.get("profile", "")),
            )
        except ValueError as e:
            _log.warning("routing.rules: invalid rule %r: %s", raw_rule, e)
            continue
        parsed_rules.append(rule_obj)

    default_block = block.get("default") or {}
    if not isinstance(default_block, dict):
        _log.warning(
            "routing.default: expected mapping, got %r — using built-in default",
            type(default_block),
        )
        default_obj = RoutingDefault()
    else:
        default_obj = RoutingDefault(
            agent=str(default_block.get("agent", "default")) or "default",
            profile=str(default_block.get("profile", "")),
        )

    return RoutingConfig(
        rules=sort_rules_by_specificity(tuple(parsed_rules)),
        default=default_obj,
    )


def load_config(path: Path | None = None) -> Config:
    """Load config from YAML, applying overrides on top of defaults.

    Missing file or empty file → returns defaults. Invalid YAML is an error.

    III.6 — the top-level ``hooks:`` block is parsed via
    :func:`_parse_hooks_block` and merged into :attr:`Config.hooks`.
    """
    cfg_path = path or config_file_path()
    base = default_config()
    try:
        raw = load_yaml_dict(cfg_path)
    except ConfigYAMLError as exc:
        raise RuntimeError(f"Failed to parse {cfg_path}: {exc.cause}") from exc
    if not raw:
        return base

    # Extract and parse the hooks block before applying regular overrides
    # (so the nested/list shape doesn't go through _apply_overrides, which
    # only knows about flat tuple-of-dataclasses).
    hooks_block = raw.pop("hooks", None)
    parsed_hooks = _parse_hooks_block(hooks_block)
    # v1.1 plan-2 M8.1 (2026-05-09) — same YAML block also feeds prompt
    # hooks. Each parser sniffs `type:` and only consumes entries it
    # understands.
    parsed_prompt_hooks = _parse_prompt_hooks_block(hooks_block)
    # v1.1 plan-2 M8.2 (2026-05-09) — and agent hooks.
    parsed_agent_hooks = _parse_agent_hooks_block(hooks_block)
    # v1.1 plan-3 M10.1 (2026-05-09) — top-level `routing:` block.
    routing_block = raw.pop("routing", None)
    parsed_routing = _parse_routing_block(routing_block)

    # G9 (Hermes parity, 2026-05-09) — normalize the nested
    # ``tools: {include, exclude, prompts, resources}`` form into the
    # flat dataclass-field form before _apply_overrides walks it.
    mcp_block = raw.get("mcp")
    if isinstance(mcp_block, dict):
        servers_block = mcp_block.get("servers")
        if isinstance(servers_block, list):
            mcp_block["servers"] = [
                _normalize_mcp_server_dict(s) if isinstance(s, dict) else s
                for s in servers_block
            ]

    cfg = _apply_overrides(base, raw)
    if parsed_hooks or parsed_prompt_hooks or parsed_agent_hooks or parsed_routing:
        kwargs = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
        if parsed_hooks:
            kwargs["hooks"] = parsed_hooks
        if parsed_prompt_hooks:
            kwargs["prompt_hooks"] = parsed_prompt_hooks
        if parsed_agent_hooks:
            kwargs["agent_hooks"] = parsed_agent_hooks
        if parsed_routing is not None:
            kwargs["routing"] = parsed_routing
        cfg = Config(**kwargs)
    return cfg


# ─── save ────────────────────────────────────────────────────────


def _to_yaml_dict(cfg: Config) -> dict[str, Any]:
    """Convert a Config dataclass to a YAML-friendly dict (Paths as strings)."""

    def _encode(v: Any) -> Any:
        if isinstance(v, Path):
            return str(v)
        if is_dataclass(v):
            return {k: _encode(getattr(v, k)) for k in [f.name for f in fields(v)]}
        if isinstance(v, tuple):
            return [_encode(item) for item in v]
        if isinstance(v, dict):
            # Recurse so nested dataclasses (e.g.
            # ``CustomProvider.models[<id>] = CustomProviderModelOverride``)
            # are encoded into plain dicts that yaml.SafeDumper can write.
            return {k: _encode(item) for k, item in v.items()}
        return v

    result: dict[str, Any] = {
        "model": _encode(cfg.model),
        "loop": _encode(cfg.loop),
        "session": _encode(cfg.session),
        "memory": _encode(cfg.memory),
        "mcp": _encode(cfg.mcp),
        "tools": _encode(cfg.tools),
        "deepening": _encode(cfg.deepening),
        "gateway": _encode(cfg.gateway),
        "system_control": _encode(cfg.system_control),
    }
    # Wave 3 — only serialize the new top-level fields when non-empty
    # / non-default so existing configs stay tidy. Each round-trips
    # through the auto-parser.
    if cfg.custom_providers:
        result["custom_providers"] = [_encode(cp) for cp in cfg.custom_providers]
    # Compare against a freshly-constructed default to detect any non-
    # default ProviderRoutingConfig field. Cheap; no cost when default.
    if cfg.provider_routing != type(cfg.provider_routing)():
        result["provider_routing"] = _encode(cfg.provider_routing)
    if cfg.fallback_providers:
        result["fallback_providers"] = [_encode(fp) for fp in cfg.fallback_providers]
    if cfg.model_context_overrides:
        result["model_context_overrides"] = dict(cfg.model_context_overrides)
    # III.6 — only serialise the hooks block when non-empty so default
    # configs stay tidy. Shape matches the nested event-keyed form users
    # write by hand (see _parse_hooks_block for the round-trip contract).
    if cfg.hooks:
        hooks_by_event: dict[str, list[dict[str, Any]]] = {}
        for h in cfg.hooks:
            entry: dict[str, Any] = {"command": h.command}
            if h.matcher is not None:
                entry["matcher"] = h.matcher
            entry["timeout_seconds"] = h.timeout_seconds
            hooks_by_event.setdefault(h.event, []).append(entry)
        result["hooks"] = hooks_by_event
    # T6 — Hermes-doc credential_pool_strategies. Only emit when
    # non-empty so default configs stay tidy.
    if getattr(cfg, "credential_pool_strategies", None):
        result["credential_pool_strategies"] = dict(cfg.credential_pool_strategies)
    # v1.1 plan-3 M10.1 — routing rules. Round-trips through
    # _parse_routing_block. Only emit when non-default so existing
    # configs stay clean.
    routing = getattr(cfg, "routing", None)
    if routing is not None and (
        routing.rules or routing.default != type(routing.default)()
    ):
        routing_dict: dict[str, Any] = {}
        if routing.rules:
            rules_out = []
            for r in routing.rules:
                m = r.match
                match_dict = {
                    k: getattr(m, k)
                    for k in ("platform", "chat_id", "channel", "guild",
                              "team", "account", "role", "peer")
                    if getattr(m, k)
                }
                rule_out: dict[str, Any] = {"agent": r.agent}
                if match_dict:
                    rule_out["match"] = match_dict
                if r.profile:
                    rule_out["profile"] = r.profile
                rules_out.append(rule_out)
            routing_dict["rules"] = rules_out
        if routing.default != type(routing.default)():
            default_out: dict[str, Any] = {"agent": routing.default.agent}
            if routing.default.profile:
                default_out["profile"] = routing.default.profile
            routing_dict["default"] = default_out
        if routing_dict:
            result["routing"] = routing_dict
    # 2026-05-10 — pinned files (Optimize Grade E mitigation). Only
    # serialize when non-default so first-run configs stay clean.
    prompt_cfg = getattr(cfg, "prompt", None)
    if prompt_cfg is not None and (
        prompt_cfg.pinned_files
        or prompt_cfg.max_total_bytes != type(prompt_cfg)().max_total_bytes
    ):
        prompt_dict: dict[str, Any] = {}
        if prompt_cfg.pinned_files:
            prompt_dict["pinned_files"] = list(prompt_cfg.pinned_files)
        if prompt_cfg.max_total_bytes != type(prompt_cfg)().max_total_bytes:
            prompt_dict["max_total_bytes"] = prompt_cfg.max_total_bytes
        if prompt_dict:
            result["prompt"] = prompt_dict
    return result


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Write config to YAML. Returns the path written."""
    cfg_path = path or config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = _to_yaml_dict(cfg)
    cfg_path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return cfg_path


# ─── get / set by dotted key ────────────────────────────────────


def get_value(cfg: Config, key: str) -> Any:
    """Get a value by dotted key like 'model.provider'."""
    parts = key.split(".")
    current: Any = cfg
    for p in parts:
        if is_dataclass(current):
            if not hasattr(current, p):
                raise KeyError(f"Unknown config key: {key} (failed at '{p}')")
            current = getattr(current, p)
        else:
            raise KeyError(f"Unknown config key: {key} (not a config section at '{p}')")
    return current


def set_value(cfg: Config, key: str, value: Any) -> Config:
    """Return a NEW Config with `key` set to `value`. Dotted key supported."""
    parts = key.split(".")
    if len(parts) == 1:
        raise KeyError("Top-level set not supported: use e.g. 'model.provider'")
    section_name, *rest = parts
    if not hasattr(cfg, section_name):
        raise KeyError(f"Unknown section: {section_name}")

    section = getattr(cfg, section_name)
    if not is_dataclass(section):
        raise KeyError(f"'{section_name}' is not a config section")

    # Descend into nested sections (rare in this flat schema but future-proof)
    section_overrides: dict[str, Any] = {}
    cursor = section_overrides
    for i, p in enumerate(rest):
        if i == len(rest) - 1:
            cursor[p] = value
        else:
            cursor[p] = {}
            cursor = cursor[p]

    new_section = _apply_overrides(section, section_overrides)
    kwargs = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
    kwargs[section_name] = new_section
    return Config(**kwargs)


__all__ = [
    "ConfigYAMLError",
    "config_file_path",
    "get_value",
    "load_config",
    "load_yaml_dict",
    "save_config",
    "set_value",
    "_parse_hooks_block",
]
