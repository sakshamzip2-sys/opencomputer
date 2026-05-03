"""Static checks for an adapter source file.

Used by ``Browser(action="adapter_validate")`` and the runner's boot
checks. Validates without executing the adapter's ``run`` body — we
only check that the module imports cleanly and the ``@adapter``
decorator's metadata is well-formed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ._decorator import (
    AdapterArg,
    AdapterSpec,
    get_adapter,
    get_registered_adapters,
)
from ._discovery import _import_adapter_file
from ._strategy import Strategy


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    spec: AdapterSpec | None = None


def validate_adapter_file(
    path: Path,
    *,
    skip_import: bool = False,
) -> ValidationResult:
    """Run static + dynamic checks on a single adapter file.

    Steps:
      1. AST parse — the file is syntactically valid Python.
      2. Locate a ``@adapter(...)`` decorator + ``async def run(args, ctx)``
         in the source.
      3. (Unless ``skip_import``) import the module so the decorator
         actually executes; pull the registered spec.
      4. Cross-check: site/name aren't duplicated against another
         already-registered adapter.
    """
    path = Path(path).resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return ValidationResult(False, [f"file not found: {path}"], [], None)
    if path.suffix != ".py":
        return ValidationResult(False, [f"not a .py file: {path}"], [], None)

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationResult(False, [f"cannot read {path}: {exc}"], [], None)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return ValidationResult(False, [f"syntax error: {exc}"], [], None)

    # 2) AST-level checks — locate @adapter decorator + async def run
    has_adapter_decorator = False
    has_async_run = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if _is_adapter_decorator(dec):
                    has_adapter_decorator = True
                    if not isinstance(node, ast.AsyncFunctionDef):
                        errors.append(
                            f"@adapter target {node.name!r} must be `async def`"
                        )
                    if len(node.args.args) < 2:
                        errors.append(
                            f"@adapter target {node.name!r} must accept (args, ctx) — "
                            f"got {len(node.args.args)} param(s)"
                        )
                    if node.name == "run":
                        has_async_run = True
                    break

    if not has_adapter_decorator:
        errors.append(
            "no @adapter-decorated function found "
            "(import `from extensions.adapter_runner import adapter, Strategy`)"
        )
    if has_adapter_decorator and not has_async_run:
        warnings.append(
            "decorated function is not named `run` — convention is "
            "`async def run(args, ctx)`"
        )

    # 3) Dynamic: import the file + pull the spec
    spec: AdapterSpec | None = None
    if not skip_import and not errors:
        # Snapshot registry pre-import so we can find the new spec
        existing = {(s.site, s.name) for s in get_registered_adapters()}
        import_err = _import_adapter_file(path, prefix="validate")
        if import_err:
            errors.append(import_err)
        else:
            for s in get_registered_adapters():
                if (s.site, s.name) not in existing:
                    spec = s
                    break
            if spec is None:
                # Adapter was already registered (validate after first
                # discovery). Pull it via the registry.
                # We need to find which spec lives in this file.
                for s in get_registered_adapters():
                    if s.source_path == path:
                        spec = s
                        break
            if spec is None:
                errors.append("file imported cleanly but no @adapter spec registered")

    # 4) Spec checks (only when we have one)
    if spec is not None:
        if not isinstance(spec.strategy, Strategy):
            errors.append(
                f"strategy must be one of {[s.value for s in Strategy]}"
            )
        if not spec.columns:
            warnings.append("columns=[] — declaring expected columns helps the agent")
        for arg in spec.args:
            if not isinstance(arg, AdapterArg):
                errors.append(f"bad arg entry: {arg!r}")

    return ValidationResult(
        ok=not errors, errors=errors, warnings=warnings, spec=spec
    )


def _is_adapter_decorator(node: ast.expr) -> bool:
    """Return True if the AST node looks like ``@adapter(...)``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name) and target.id == "adapter":
        return True
    return isinstance(target, ast.Attribute) and target.attr == "adapter"


def check_no_duplicates() -> list[str]:
    """Scan the registry for duplicate (site, name) pairs.

    The decorator already prevents same-spec re-registration, but a
    second adapter file declaring an identical pair would have raised
    on the second import. This helper exists for the doctor row to
    report any registered adapters whose tool_name collides.
    """
    seen: dict[str, str] = {}
    errors: list[str] = []
    for spec in get_registered_adapters():
        if spec.tool_name in seen:
            errors.append(
                f"duplicate tool name {spec.tool_name!r}: "
                f"{seen[spec.tool_name]} vs {spec.source_path}"
            )
        else:
            seen[spec.tool_name] = str(spec.source_path)
    return errors


__all__ = [
    "ValidationResult",
    "check_no_duplicates",
    "get_adapter",
    "validate_adapter_file",
]
