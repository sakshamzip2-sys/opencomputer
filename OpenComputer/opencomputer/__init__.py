"""OpenComputer — personal AI agent framework."""
from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("opencomputer")
except _metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"
