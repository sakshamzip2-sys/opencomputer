"""Phase 12b.2 — Sub-project B Task B1.

Renderer behind ``opencomputer plugin new`` (B2 wires it into the CLI;
B3 adds smoke). Given a plugin id + kind, it expands the template tree
under ``opencomputer/templates/plugin/<kind>/`` into a working plugin
skeleton on disk.

The templates themselves live as ``.j2`` files next to this module —
see ``opencomputer/templates/plugin/{channel,provider,toolkit,mixed}/``.
Both file contents AND file names are rendered with Jinja2, so entries
like ``tests/test_{{ module_name }}.py.j2`` expand to
``tests/test_<module_name>.py`` in the output.

Template variables exposed to all templates:

- ``plugin_id`` — the raw id passed in, e.g. ``"weather-demo"``.
- ``plugin_name`` — human-readable display name (defaults to a Title
  Case version of ``plugin_id``).
- ``description`` / ``author`` — free-form strings, default to ``""``.
- ``module_name`` — python-safe identifier: ``plugin_id.replace("-", "_")``.
- ``class_name`` — PascalCase identifier for class stubs.
- ``kind`` — the CLI-level kind (``"channel"``, ``"provider"``,
  ``"toolkit"``, or ``"mixed"``).

Note on ``kind`` mapping: the CLI uses ``"toolkit"`` for UX clarity,
but the plugin manifest must record the SDK value ``"tool"`` (see
``plugin_sdk.core.PluginManifest``). The mapping is handled inside the
toolkit template directly — the template's ``plugin.json.j2`` hard-codes
``"kind": "tool"``. See the per-kind templates for ground truth.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Final, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined

#: Same id pattern the manifest validator uses — keep these in sync.
#: See ``opencomputer/plugins/manifest_validator.py`` for the canonical copy.
_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
)

#: Root of the installed templates directory. Templates live under
#: ``<TEMPLATES_ROOT>/plugin/<kind>/`` for each supported kind.
TEMPLATES_ROOT: Final[Path] = Path(__file__).resolve().parent / "templates"

#: The kinds the CLI accepts. Note "toolkit" is UX sugar for the SDK's
#: ``"tool"`` kind — mapping lives in ``toolkit/plugin.json.j2``.
PluginKind = Literal["channel", "provider", "toolkit", "mixed"]

_VALID_KINDS: Final[tuple[str, ...]] = ("channel", "provider", "toolkit", "mixed")


def _derive_module_name(plugin_id: str) -> str:
    """plugin_id uses hyphens; python identifiers don't. Swap them."""
    return plugin_id.replace("-", "_")


def _derive_class_name(plugin_id: str) -> str:
    """PascalCase the id for class stubs: ``foo-bar-baz`` -> ``FooBarBaz``."""
    parts = plugin_id.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


def _default_plugin_name(plugin_id: str) -> str:
    """Title-case fallback: ``my-weather`` -> ``My Weather``."""
    parts = plugin_id.replace("-", " ").replace("_", " ").split()
    return " ".join(p.capitalize() for p in parts if p)


def render_plugin_template(
    *,
    plugin_id: str,
    kind: PluginKind,
    output_path: Path,
    name: str | None = None,
    description: str = "",
    author: str = "",
    overwrite: bool = False,
) -> list[Path]:
    """Render the plugin template tree into ``output_path/plugin_id/``.

    Args:
        plugin_id: Lowercase letters/digits/hyphens, 1-64 chars — same
            format the manifest validator enforces.
        kind: One of ``"channel" | "provider" | "toolkit" | "mixed"``.
            ``"toolkit"`` maps to the SDK's ``"tool"`` kind in the
            rendered manifest (see module docstring).
        output_path: Parent directory; the plugin is written into
            ``<output_path>/<plugin_id>/``.
        name: Display name; defaults to a Title Case version of the id.
        description: Free-form description for manifest + README.
        author: Free-form author string for manifest + README.
        overwrite: If ``True``, any existing target dir is removed before
            rendering. Default ``False`` raises ``FileExistsError``.

    Returns:
        Absolute paths of every file written, in the order they were
        rendered.

    Raises:
        ValueError: ``plugin_id`` fails the id regex, or ``kind`` is not
            in ``_VALID_KINDS``.
        FileExistsError: Target directory exists and ``overwrite=False``.
        FileNotFoundError: Template directory for ``kind`` is missing
            (install bug — indicates the package shipped incomplete).
    """
    if not _ID_RE.match(plugin_id):
        raise ValueError(
            f"plugin id {plugin_id!r} must be lowercase letters/digits/hyphens, "
            f"start+end with alphanumeric, 1-64 chars"
        )
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"kind {kind!r} must be one of {_VALID_KINDS}"
        )

    template_dir = TEMPLATES_ROOT / "plugin" / kind
    if not template_dir.is_dir():
        raise FileNotFoundError(
            f"template directory missing for kind={kind!r}: {template_dir} "
            f"(package install may be incomplete)"
        )

    target_root = Path(output_path) / plugin_id
    if target_root.exists():
        if not overwrite:
            raise FileExistsError(
                f"target already exists: {target_root} "
                f"(pass overwrite=True to replace)"
            )
        shutil.rmtree(target_root)

    module_name = _derive_module_name(plugin_id)
    class_name = _derive_class_name(plugin_id)
    display_name = name if name else _default_plugin_name(plugin_id)

    context: dict[str, str] = {
        "plugin_id": plugin_id,
        "plugin_name": display_name,
        "description": description,
        "author": author,
        "module_name": module_name,
        "class_name": class_name,
        "kind": kind,
    }

    # Two Environments: one for file contents (loads relative to the
    # kind dir) and one for filename strings (no loader needed — we pass
    # the raw template string). Both use StrictUndefined so any typo in
    # a template blows up loudly during dev rather than silently
    # rendering an empty string.
    content_env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    filename_env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
    )

    written: list[Path] = []
    for template_path in sorted(template_dir.rglob("*.j2")):
        rel = template_path.relative_to(template_dir)
        # Render every path segment that contains Jinja syntax. The
        # ``.j2`` extension on the final segment is stripped after
        # rendering the filename.
        rendered_parts: list[str] = []
        for part in rel.parts:
            if "{{" in part or "{%" in part:
                rendered_parts.append(filename_env.from_string(part).render(**context))
            else:
                rendered_parts.append(part)
        rel_rendered = Path(*rendered_parts)
        # Strip the trailing ".j2" suffix on the final component.
        final_name = rel_rendered.name
        if final_name.endswith(".j2"):
            final_name = final_name[: -len(".j2")]
        out_path = target_root / rel_rendered.parent / final_name

        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Load via posix-style relative path for cross-platform safety —
        # Jinja2's FileSystemLoader expects forward slashes.
        template = content_env.get_template(rel.as_posix())
        rendered = template.render(**context)
        out_path.write_text(rendered, encoding="utf-8")
        written.append(out_path.resolve())

    return written


__all__ = [
    "TEMPLATES_ROOT",
    "PluginKind",
    "render_plugin_template",
]
