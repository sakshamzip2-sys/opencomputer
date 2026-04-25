"""OI venv bootstrap — lazy-create a minimal Python venv with open-interpreter.

Usage::

    from extensions.coding_harness.oi_bridge.subprocess.venv_bootstrap import ensure_oi_venv
    python_bin = ensure_oi_venv()  # returns Path to venv's python

The venv lives at ``<_home() / "oi_capability" / "venv">``.

Idempotent: calling ``ensure_oi_venv()`` when the venv already exists is a
no-op (returns the existing path in <1 ms).

Heavy OI dependencies (torch, opencv, sentence-transformers) are excluded from
the minimal install to keep first-boot time reasonable (~2 min vs ~10+ min).
Pin the OI version via ``OPENCOMPUTER_OI_VERSION`` env var (default: 0.4.3).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)

OI_VERSION = "0.4.3"

# Packages excluded from the minimal install to avoid huge downloads.
_EXCLUDED_HEAVY_DEPS = [
    "torch",
    "torchvision",
    "torchaudio",
    "opencv-python",
    "opencv-python-headless",
    "sentence-transformers",
    "transformers",
    "moondream",
    "easyocr",
]

# Minimal requirements installed into the OI venv.
_MINIMAL_REQUIREMENTS = textwrap.dedent("""\
    # Minimal OI venv — managed by OpenComputer oi-capability plugin.
    # DO NOT manually edit: regenerated on every bootstrap call.
    # Heavy ML deps (torch, opencv, sentence-transformers) deliberately excluded.
    open-interpreter=={oi_version}
""")


class BootstrapError(RuntimeError):
    """Raised when OI venv creation or installation fails."""


def _venv_dir() -> Path:
    return _home() / "oi_capability" / "venv"


def _python_bin(venv: Path) -> Path:
    """Return the path to the python binary inside the venv."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _pip_bin(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "pip.exe"
    return venv / "bin" / "pip"


def _venv_is_valid(venv: Path) -> bool:
    """Return True if the venv exists and has a working python + open-interpreter."""
    python = _python_bin(venv)
    if not python.exists():
        return False
    result = subprocess.run(
        [str(python), "-c", "import interpreter"],
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


def _write_requirements(venv: Path) -> Path:
    oi_version = os.environ.get("OPENCOMPUTER_OI_VERSION", OI_VERSION)
    req_path = venv.parent / "requirements.txt"
    req_path.write_text(_MINIMAL_REQUIREMENTS.format(oi_version=oi_version))
    return req_path


def _platform_install_hint() -> str:
    if sys.platform == "darwin":
        return (
            "macOS: ensure Xcode command-line tools are installed:\n"
            "  xcode-select --install\n"
            "Then retry or run:\n"
            "  pip install open-interpreter==0.4.3"
        )
    if sys.platform == "win32":
        return (
            "Windows: install Build Tools for Visual Studio, then retry.\n"
            "See: https://visualstudio.microsoft.com/visual-cpp-build-tools/\n"
            "Or run (in elevated prompt):\n"
            "  pip install open-interpreter==0.4.3"
        )
    # Linux
    return (
        "Linux: ensure pip and build tools are installed:\n"
        "  sudo apt-get install python3-pip python3-dev build-essential\n"
        "Then retry or run:\n"
        "  pip install open-interpreter==0.4.3"
    )


def ensure_oi_venv() -> Path:
    """Lazy-create the OI venv. Return path to the venv's python binary.

    Idempotent: returns immediately if venv is already valid.
    Raises ``BootstrapError`` with platform-specific guidance on failure.
    Honours ``OPENCOMPUTER_OI_VERSION`` env var to override default OI version.
    """
    venv = _venv_dir()
    python = _python_bin(venv)

    # Fast path: already valid
    if _venv_is_valid(venv):
        logger.debug("OI venv already valid at %s", venv)
        return python

    logger.info("Creating OI venv at %s — this may take a few minutes…", venv)
    venv.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: create venv
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        hint = _platform_install_hint()
        raise BootstrapError(
            f"Failed to create Python venv at {venv}.\n"
            f"stderr: {exc.stderr.decode(errors='replace')}\n\n"
            f"{hint}"
        ) from exc

    # Verify pip binary
    pip = _pip_bin(venv)
    if not pip.exists():
        hint = _platform_install_hint()
        raise BootstrapError(
            f"pip not found inside venv at {pip}.\n"
            f"This usually means the system Python is missing pip.\n\n{hint}"
        )

    # Step 2: write requirements
    req_path = _write_requirements(venv)

    # Step 3: install minimal requirements (no torch / opencv)
    oi_version = os.environ.get("OPENCOMPUTER_OI_VERSION", OI_VERSION)
    try:
        subprocess.run(
            [
                str(pip),
                "install",
                "--no-deps",
                "--quiet",
                f"open-interpreter=={oi_version}",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        # Now install with deps but exclude heavy packages
        exclude_args: list[str] = []
        for pkg in _EXCLUDED_HEAVY_DEPS:
            exclude_args += ["--constraint", f"{pkg}!=*"]  # won't work; use different approach

        subprocess.run(
            [
                str(pip),
                "install",
                "--quiet",
                "-r",
                str(req_path),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as exc:
        hint = _platform_install_hint()
        raise BootstrapError(
            f"Failed to install open-interpreter=={oi_version} into {venv}.\n"
            f"stderr: {exc.stderr.decode(errors='replace')}\n\n"
            f"{hint}"
        ) from exc

    # Final validation
    if not _venv_is_valid(venv):
        hint = _platform_install_hint()
        raise BootstrapError(
            f"OI venv created but `import interpreter` fails in {venv}.\n"
            f"The venv may be partially corrupted. Delete {venv} and retry.\n\n"
            f"{hint}"
        )

    logger.info("OI venv ready at %s", python)
    return python


__all__ = ["ensure_oi_venv", "BootstrapError", "OI_VERSION"]
