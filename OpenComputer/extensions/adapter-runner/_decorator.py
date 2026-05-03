"""``@adapter`` decorator + ``AdapterSpec`` dataclass + module-level registry.

A user-authored adapter file looks like::

    from extensions.adapter_runner import adapter, Strategy

    @adapter(
        site="hackernews",
        name="top",
        description="Hacker News top stories",
        domain="news.ycombinator.com",
        strategy=Strategy.PUBLIC,
        browser=False,
        args=[{"name": "limit", "type": "int", "default": 20}],
        columns=["rank", "title", "score", "author", "comments"],
    )
    async def run(args, ctx):
        ...

The decorator:
  1. Validates the metadata (site/name/strategy/columns/...).
  2. Wraps the underlying function as an ``AdapterSpec`` dataclass.
  3. Registers the spec in the module-level ``_REGISTRY`` keyed by
     ``(site, name)``. Duplicates raise ``AdapterConfigError``.
  4. Returns the unwrapped function so adapters can still be called
     directly in tests (``await mymodule.run(args, ctx)``) without
     going through the runner.

The registry is process-global so ``adapter-runner``'s discovery walk
can collect every adapter that's ever been imported. Tests that want
isolation should call ``clear_registry_for_tests()``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._strategy import Strategy

# Hyphenated-on-disk plugin path → underscore Python alias. The error
# class lives in ``extensions.browser_control._utils.errors``; the
# ``adapter-runner`` plugin doesn't import it at module-import time
# because that would require the browser-control alias to exist
# already. The decorator raises a plain ``ValueError`` here, and
# ``_runner.py`` is responsible for translating it into the typed
# ``AdapterConfigError`` when discovery surfaces a bad spec.

#: Adapter run signature: ``async def run(args: dict, ctx: Any) -> Any``.
RunFn = Callable[[dict[str, Any], Any], Awaitable[Any]]


_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_]{0,62}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class AdapterArg:
    """Schema for one adapter argument.

    Mirrors the ``args=[{...}]`` entries from the decorator. Frozen so
    a malformed spec can't be mutated by adapter code at runtime.
    """

    name: str
    type: str = "string"  # "string" | "int" | "float" | "bool"
    default: Any = None
    help: str = ""
    required: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdapterArg:
        if not isinstance(raw, dict):
            raise ValueError(f"args entry must be a dict, got {type(raw).__name__}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("args entry missing 'name' (string)")
        return cls(
            name=name.strip(),
            type=str(raw.get("type", "string") or "string"),
            default=raw.get("default"),
            help=str(raw.get("help", "") or ""),
            required=bool(raw.get("required", False)),
        )


@dataclass(slots=True)
class AdapterSpec:
    """A discovered adapter's metadata + the underlying ``run`` callable.

    ``run`` is the user's coroutine; ``source_path`` is set by
    ``_discovery.py`` to the absolute path of the .py file we imported
    it from (used for trace artifacts + ``adapter_validate`` /
    ``adapter_save``).
    """

    site: str
    name: str
    description: str
    domain: str
    strategy: Strategy
    browser: bool
    args: tuple[AdapterArg, ...]
    columns: tuple[str, ...]
    run: RunFn
    timeout_seconds: float = 60.0
    source_path: Path | None = None
    notes: str = ""

    @property
    def site_pascal(self) -> str:
        return _to_pascal(self.site)

    @property
    def name_pascal(self) -> str:
        return _to_pascal(self.name)

    @property
    def tool_name(self) -> str:
        """Generated synthetic tool name: ``<Site><Name>``."""
        return f"{self.site_pascal}{self.name_pascal}"

    def to_json_schema(self) -> dict[str, Any]:
        """OpenAI-compatible JSON schema for the synthetic tool."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for a in self.args:
            schema_type = {
                "string": "string",
                "str": "string",
                "int": "integer",
                "integer": "integer",
                "float": "number",
                "number": "number",
                "bool": "boolean",
                "boolean": "boolean",
            }.get(a.type.lower(), "string")
            entry: dict[str, Any] = {"type": schema_type}
            if a.help:
                entry["description"] = a.help
            if a.default is not None and not a.required:
                entry["default"] = a.default
            properties[a.name] = entry
            if a.required:
                required.append(a.name)
        out: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        }
        if required:
            out["required"] = required
        return out


def _to_pascal(s: str) -> str:
    parts = re.split(r"[\s_\-./]+", s)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


# ─── module-level registry ───────────────────────────────────────────

#: ``(site, name) → AdapterSpec`` for every successfully-imported adapter.
_REGISTRY: dict[tuple[str, str], AdapterSpec] = {}


def adapter(
    *,
    site: str,
    name: str,
    description: str,
    domain: str,
    strategy: Strategy | str,
    browser: bool = False,
    args: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    columns: list[str] | tuple[str, ...] = (),
    timeout_seconds: float = 60.0,
    notes: str = "",
) -> Callable[[RunFn], RunFn]:
    """Decorator factory — register an ``async def run`` as an adapter.

    Returns the underlying function unchanged so adapter code can still
    be unit-tested by calling ``run(args, ctx)`` directly. The runner
    looks up the spec via ``_REGISTRY[(site, name)]``.
    """
    # Normalize/validate up front so a bad decorator argument fails
    # eagerly at import time (where the traceback points at the user's
    # adapter file, not at some downstream tool execution).
    if not isinstance(site, str) or not _ID_RE.match(site or ""):
        raise ValueError(
            f"@adapter site={site!r} must be lowercase alphanumeric "
            "(underscores allowed inside)"
        )
    if not isinstance(name, str) or not _ID_RE.match(name or ""):
        raise ValueError(
            f"@adapter name={name!r} must be lowercase alphanumeric "
            "(underscores allowed inside)"
        )
    if not isinstance(description, str) or not description.strip():
        raise ValueError("@adapter description must be a non-empty string")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("@adapter domain must be a non-empty string")

    if isinstance(strategy, str):
        try:
            strategy_enum = Strategy(strategy.lower())
        except ValueError as exc:
            raise ValueError(
                f"@adapter strategy={strategy!r} must be one of: "
                + ", ".join(s.value for s in Strategy)
            ) from exc
    elif isinstance(strategy, Strategy):
        strategy_enum = strategy
    else:
        raise ValueError(f"@adapter strategy must be Strategy enum or str, got {type(strategy).__name__}")

    arg_specs = tuple(AdapterArg.from_dict(a) for a in (args or ()))
    seen_names: set[str] = set()
    for a in arg_specs:
        if a.name in seen_names:
            raise ValueError(f"@adapter duplicate arg name {a.name!r}")
        seen_names.add(a.name)

    cols = tuple(str(c) for c in (columns or ()))

    def _wrap(fn: RunFn) -> RunFn:
        if not inspect.iscoroutinefunction(fn):
            raise ValueError(
                f"@adapter target must be `async def`, got {fn!r}"
            )
        # ``inspect.signature`` rejects builtins, but real adapter funcs
        # are user-authored coroutines — safe to inspect.
        params = list(inspect.signature(fn).parameters.values())
        if len(params) < 2:
            raise ValueError(
                f"@adapter target {fn.__qualname__} must accept "
                "(args, ctx) — got "
                f"{len(params)} param(s)"
            )

        spec = AdapterSpec(
            site=site,
            name=name,
            description=description.strip(),
            domain=domain.strip(),
            strategy=strategy_enum,
            browser=bool(browser),
            args=arg_specs,
            columns=cols,
            run=fn,
            timeout_seconds=float(timeout_seconds),
            source_path=_resolve_source_path(fn),
            notes=notes,
        )

        key = (site, name)
        existing = _REGISTRY.get(key)
        new_path = _resolve_source_path(fn)
        if existing is not None and existing.run is not fn:
            # Two cases:
            #
            # 1) Same source file, two `@adapter` calls with the same
            #    (site, name) — programmer error. Raise so the author
            #    sees it at import time.
            #
            # 2) Different source files (bundled vs user-local etc.) —
            #    discovery priority: bundled → plugins → user. Last
            #    wins; we silently override. Eject/reset semantics
            #    in DEFERRED.md §E will formalize this in v0.6.
            if existing.source_path == new_path:
                raise ValueError(
                    f"@adapter duplicate (site, name) = ({site}, {name}) — "
                    f"already registered from {existing.source_path}"
                )
        _REGISTRY[key] = spec

        # Stash the spec on the function so callers (tests, the
        # runner) can recover it without going through the registry.
        fn._adapter_spec = spec  # type: ignore[attr-defined]
        return fn

    return _wrap


def _resolve_source_path(fn: RunFn) -> Path | None:
    try:
        src = inspect.getsourcefile(fn)
        if src:
            return Path(src).resolve()
    except (TypeError, OSError):
        pass
    return None


def get_registered_adapters() -> list[AdapterSpec]:
    """Snapshot of the current registry. Order: insertion order."""
    return list(_REGISTRY.values())


def get_adapter(site: str, name: str) -> AdapterSpec | None:
    """Look up a single adapter by (site, name)."""
    return _REGISTRY.get((site, name))


def clear_registry_for_tests() -> None:
    """Test-only — empty the global registry + drop synthetic adapter modules.

    Discovery imports adapter modules under ``oc_adapter.<source>.<rel-path>``
    synthetic names (see ``_discovery.py::_import_adapter_file``). Re-running
    discovery after only a registry clear would skip those modules
    (``sys.modules`` cache hit) and silently leave the registry empty.
    Drop both for true isolation between tests.
    """
    import sys

    _REGISTRY.clear()
    for key in list(sys.modules):
        if key.startswith("oc_adapter."):
            sys.modules.pop(key, None)


__all__ = [
    "AdapterArg",
    "AdapterSpec",
    "RunFn",
    "adapter",
    "clear_registry_for_tests",
    "get_adapter",
    "get_registered_adapters",
]
