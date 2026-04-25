# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Autonomous code refactoring / migration helper.

Integration with ``extensions/coding-harness/*`` is Session A's Phase 5 scope.
This module composes OI tools standalone; the actual coding-harness bridge
wiring happens during the interweaving refactor per
``docs/f7/interweaving-plan.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..tools.tier_1_introspection import ReadFileRegionTool, SearchFilesTool
from ..tools.tier_4_system_control import EditFileTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper


async def plan_refactor(
    wrapper: OISubprocessWrapper,
    target_dir: str,
    refactor_description: str,
) -> dict:
    """Search for candidate files in *target_dir* and estimate the scope.

    Uses :class:`SearchFilesTool` (Tier 1) to find candidate files that match
    the refactor description query.

    Returns::

        {
            "candidates": ["/abs/path/file.py", ...],
            "estimated_changes": int,
        }
    """
    tool = SearchFilesTool(wrapper=wrapper)

    from plugin_sdk.core import ToolCall

    call = ToolCall(
        id="plan-refactor",
        name="search_files",
        arguments={"query": refactor_description, "directory": target_dir},
    )
    result = await tool.execute(call)

    if result.is_error:
        return {"candidates": [], "estimated_changes": 0}

    # Parse the raw content — the tool returns a string representation of a list
    raw = result.content
    candidates: list[str] = []
    if raw and raw.strip() not in ("", "{}"):
        # Try to parse list repr; fall back to splitting on newlines
        try:
            import ast

            parsed = ast.literal_eval(raw)
            candidates = [str(p) for p in parsed] if isinstance(parsed, list) else [raw.strip()]
        except (ValueError, SyntaxError):
            candidates = [line.strip() for line in raw.splitlines() if line.strip()]

    return {
        "candidates": candidates,
        "estimated_changes": len(candidates),
    }


async def execute_refactor_dry_run(wrapper: OISubprocessWrapper, plan: dict) -> dict:
    """Simulate refactoring without writing to disk.

    Reads each candidate file via :class:`ReadFileRegionTool` and generates a
    stub diff string. **Does NOT call** :class:`EditFileTool` — this is a
    read-only preview.

    Returns::

        {
            "would_change": ["/abs/path/file.py", ...],
            "preview": {"/abs/path/file.py": "<diff string>"},
        }
    """
    read_tool = ReadFileRegionTool(wrapper=wrapper)

    from plugin_sdk.core import ToolCall

    would_change: list[str] = []
    preview: dict[str, str] = {}

    for candidate in plan.get("candidates", []):
        call = ToolCall(
            id=f"dry-run-read-{candidate}",
            name="read_file_region",
            arguments={"path": candidate, "offset": 0, "length": 4096},
        )
        result = await read_tool.execute(call)

        if not result.is_error and result.content.strip():
            would_change.append(candidate)
            # Stub diff — actual diff generation is coding-harness Phase 5 scope
            preview[candidate] = (
                f"--- a/{candidate}\n"
                f"+++ b/{candidate}\n"
                f"@@ -1,1 +1,1 @@\n"
                f"-<original content (first 100 chars): {result.content[:100]}>\n"
                f"+<refactored content: [stub — coding-harness wires real diff in Phase 5]>\n"
            )

    return {"would_change": would_change, "preview": preview}


async def execute_refactor(
    wrapper: OISubprocessWrapper,
    plan: dict,
    *,
    confirm: bool = False,
) -> dict:
    """Apply refactoring changes to each candidate file.

    Calls :class:`EditFileTool` for each planned change.

    Parameters
    ----------
    wrapper:
        The OI subprocess wrapper.
    plan:
        Output of :func:`plan_refactor`.
    confirm:
        **Must be** ``True`` — acts as a guard against accidental mutations.

    Raises
    ------
    ValueError
        If ``confirm`` is not ``True``.

    Returns::

        {
            "changed": ["/abs/path/file.py", ...],
            "errors": [{"path": ..., "error": ...}, ...],
        }
    """
    if not confirm:
        raise ValueError("autonomous refactor requires explicit confirm=True")

    edit_tool = EditFileTool(wrapper=wrapper)

    from plugin_sdk.core import ToolCall

    changed: list[str] = []
    errors: list[dict] = []

    for candidate in plan.get("candidates", []):
        call = ToolCall(
            id=f"refactor-edit-{candidate}",
            name="edit_file",
            arguments={
                "path": candidate,
                # Stub replacement — coding-harness provides real transformations in Phase 5
                "original_text": "# [original]",
                "replacement_text": "# [refactored by autonomous_refactor]",
            },
        )
        result = await edit_tool.execute(call)
        if result.is_error:
            errors.append({"path": candidate, "error": result.content})
        else:
            changed.append(candidate)

    return {"changed": changed, "errors": errors}
