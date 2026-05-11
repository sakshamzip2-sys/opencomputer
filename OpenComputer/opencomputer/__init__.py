"""OpenComputer — personal AI agent framework."""
# Side-effect import: hardens warning filters BEFORE any sibling submodule
# can transitively pull urllib3 / requests / aiohttp and leak a noisy
# version-mismatch UserWarning into clean CLI output. See _early_init.py
# for the full rationale. Listed first so the filter is active for every
# import path: oc CLI, oc gateway, oc wire, oneshot, tests, embedders.
from opencomputer import _early_init  # noqa: F401, I001 — side-effect-on-import, must precede other imports
from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("opencomputer")
except _metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"
