"""``GraphListDriveFilesTool`` — list OneDrive files through Microsoft Graph.

Build-chunk 3 of Milestone 3. The agent-facing tool over
:meth:`opencomputer.integrations.graph.client._DriveOperations.list`
(``GET /me/drive/root/children`` — or the path-addressed
``/me/drive/root:/{folder}:/children`` variant).

Consent tier — ``EXPLICIT``
---------------------------
This tool **reads cloud data** — the user's OneDrive. ``IMPLICIT`` (tier 0) is
"no external data read" and is therefore wrong here. ``EXPLICIT`` is correct:
reading the drive is a revocable, source-level capability granted once (the
``oc auth login graph`` OAuth consent being the source-level checkpoint).

Folder vs file
--------------
A Graph ``driveItem`` is a folder when it carries a ``folder`` (or ``package``)
facet — *not* by the absence of the ``file`` facet (some items have neither
cleanly). The result formatter discriminates on the ``folder`` / ``package``
facet, per the Graph survey.
"""

from __future__ import annotations

from typing import Any, ClassVar

from opencomputer.tools._graph_common import (
    NOT_AUTHENTICATED_MESSAGE,
    error_result,
    run_read_with_401_retry,
    tool_available,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Cap on the number of drive items returned across all pages — passed to the
#: client's paginator so a huge folder can't run unbounded into context.
_MAX_ITEMS = 200

#: Human-readable size-unit ladder for :func:`_format_size`.
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _format_size(size: Any) -> str:
    """Render a byte count as a compact human-readable string.

    Returns an empty string when ``size`` is missing or not a non-negative
    integer (folders frequently have no meaningful ``size``).
    """
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    value = float(size)
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"  # unreachable, but keeps the type-checker happy


def _is_folder(item: dict[str, Any]) -> bool:
    """Return whether a ``driveItem`` is a folder.

    Discriminates on the ``folder`` facet (a real folder) or the ``package``
    facet (OneNote-style packages — listed like folders), never on the absence
    of the ``file`` facet.
    """
    return "folder" in item or "package" in item


def _format_item(item: dict[str, Any]) -> str:
    """Render one Graph ``driveItem`` as a readable single line."""
    name = item.get("name") or "(unnamed)"
    if _is_folder(item):
        marker = "[DIR] "
        detail_parts: list[str] = []
        folder = item.get("folder")
        if isinstance(folder, dict):
            child_count = folder.get("childCount")
            if isinstance(child_count, int) and not isinstance(child_count, bool):
                detail_parts.append(
                    f"{child_count} item{'' if child_count == 1 else 's'}"
                )
        elif "package" in item:
            detail_parts.append("package")
    else:
        marker = "[FILE]"
        detail_parts = []
        size = _format_size(item.get("size"))
        if size:
            detail_parts.append(size)
        file_facet = item.get("file")
        if isinstance(file_facet, dict):
            mime = file_facet.get("mimeType")
            if isinstance(mime, str) and mime.strip():
                detail_parts.append(mime.strip())

    line = f"{marker} {name}"
    if detail_parts:
        line += f"  ({', '.join(detail_parts)})"
    modified = item.get("lastModifiedDateTime")
    if isinstance(modified, str) and modified.strip():
        line += f"  — modified {modified.strip()}"
    return line


class GraphListDriveFilesTool(BaseTool):
    """List files and folders in the signed-in Microsoft account's OneDrive."""

    # A read with no side effects — safe to run alongside other parallel tools.
    parallel_safe: bool = True

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="graph.drive.read",
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "List files and folders in your Microsoft OneDrive."
            ),
            data_scope="microsoft-graph:Files.Read",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="GraphListDriveFiles",
            description=(
                "List files and folders in the user's connected Microsoft "
                "OneDrive via Microsoft Graph (GET /me/drive/root/children). "
                "Requires the user to have run `oc auth login graph`. With no "
                "folder_path the drive root is listed; provide a path like "
                "'Documents/Reports' to list a sub-folder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "folder_path": {
                        "type": "string",
                        "description": (
                            "Optional folder path relative to the OneDrive "
                            "root, e.g. 'Documents/Reports'. Omit to list the "
                            "drive root."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:  # noqa: D102
        if not tool_available():
            return ToolResult(
                tool_call_id=call.id,
                content=NOT_AUTHENTICATED_MESSAGE,
                is_error=True,
            )

        args = call.arguments if isinstance(call.arguments, dict) else {}
        raw_path = args.get("folder_path")
        if raw_path is not None and not isinstance(raw_path, str):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "'folder_path' must be a string path, "
                    f"got {type(raw_path).__name__}"
                ),
                is_error=True,
            )
        folder_path = raw_path.strip() if isinstance(raw_path, str) else None
        if folder_path == "":
            folder_path = None

        # Token acquisition + the 401→force-refresh→retry-once policy live in
        # run_read_with_401_retry. A read is safe to retry; a send is not.
        try:
            items = await run_read_with_401_retry(
                lambda client: client.drive.list(
                    folder_path=folder_path,
                    max_items=_MAX_ITEMS,
                )
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a clean ToolResult
            return error_result(call, exc)

        return ToolResult(
            tool_call_id=call.id,
            content=self._format_result(items, folder_path),
        )

    @staticmethod
    def _format_result(
        items: list[dict[str, Any]], folder_path: str | None
    ) -> str:
        """Render the drive-item list into the tool's readable text result."""
        location = (
            f"folder '{folder_path}'" if folder_path else "the OneDrive root"
        )
        if not items:
            return f"No files or folders in {location}."

        folders = [it for it in items if _is_folder(it)]
        files = [it for it in items if not _is_folder(it)]
        header = (
            f"Contents of {location}  "
            f"({len(folders)} folder(s), {len(files)} file(s)):"
        )
        lines = [header, ""]
        # Folders first, then files — each group keeps Graph's order.
        lines.extend(_format_item(it) for it in folders)
        lines.extend(_format_item(it) for it in files)
        if len(items) >= _MAX_ITEMS:
            lines.append("")
            lines.append(
                f"(result capped at {_MAX_ITEMS} items — list a sub-folder to "
                "see more)"
            )
        return "\n".join(lines)


__all__ = ["GraphListDriveFilesTool"]
