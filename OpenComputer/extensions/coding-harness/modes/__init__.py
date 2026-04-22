"""Coding-harness modes — dynamic injection providers.

Each mode is a `DynamicInjectionProvider` that renders a Jinja2 template from
`../prompts/` when its gating flag is set on the RuntimeContext.
"""

from __future__ import annotations

from pathlib import Path
from jinja2 import Environment, FileSystemLoader

_TEMPLATE_ROOT = Path(__file__).parent.parent / "prompts"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_ROOT)),
    keep_trailing_newline=True,
    autoescape=False,
)


def render(template_name: str, **kwargs) -> str:
    """Render a template from ../prompts/."""
    return _env.get_template(template_name).render(**kwargs)


__all__ = ["render"]
