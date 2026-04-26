"""AWS Bedrock plugin registration.

PR-C of ~/.claude/plans/replicated-purring-dewdrop.md.

Note: enabled_by_default=False. Users opt in by enabling the plugin
AND installing boto3 via `pip install opencomputer[bedrock]`.
"""

# ruff: noqa: N999

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(api) -> None:
    """Register the Bedrock provider.

    Lazy-imports boto3 + the provider class to avoid hard dependency on
    boto3 at OC startup. If boto3 is missing, log + skip registration.

    The dir is named ``aws-bedrock-provider`` (hyphen) so Python can't
    import it as ``extensions.aws_bedrock_provider`` natively.
    ``tests/conftest.py`` registers an alias for the test runner;
    production needs the same alias *here*, before the import below is
    attempted. Mirrors the ``coding-harness`` pattern.
    """
    # Production-side alias for the hyphenated dir name. Idempotent —
    # safe even if conftest already registered it (test runner).
    import sys as _sys
    import types as _types
    from pathlib import Path as _Path

    if "extensions" not in _sys.modules:
        _ext_pkg = _types.ModuleType("extensions")
        _ext_pkg.__path__ = [str(_Path(__file__).resolve().parent.parent)]
        _sys.modules["extensions"] = _ext_pkg
    if "extensions.aws_bedrock_provider" not in _sys.modules:
        _bp_pkg = _types.ModuleType("extensions.aws_bedrock_provider")
        _bp_pkg.__path__ = [str(_Path(__file__).resolve().parent)]
        _bp_pkg.__package__ = "extensions.aws_bedrock_provider"
        _sys.modules["extensions.aws_bedrock_provider"] = _bp_pkg

    try:
        from extensions.aws_bedrock_provider.provider import BedrockProvider
    except ImportError as exc:
        # Distinguish boto3-missing (real opt-out path) from any other
        # import failure that would have been silently rebranded as
        # "boto3 not installed" pre-fix.
        msg = str(exc)
        if "boto3" in msg or "botocore" in msg:
            logger.info(
                "aws-bedrock-provider: boto3 not installed; skipping registration. "
                "Install with `pip install opencomputer[bedrock]`. (%s)",
                exc,
            )
        else:
            logger.warning(
                "aws-bedrock-provider: provider import failed (NOT a missing-boto3 "
                "error — investigate): %s",
                exc,
            )
        return

    try:
        # api.register_provider signature is (name: str, provider: Any).
        # Pre-fix, this called register_provider(provider) — a TypeError
        # swallowed by the broad except below + misreported as "AWS
        # credentials not configured".
        api.register_provider("bedrock", BedrockProvider)
        logger.info("aws-bedrock-provider: registered as 'bedrock'")
    except Exception as exc:  # noqa: BLE001 — defensive on opt-in plugin
        logger.warning(
            "aws-bedrock-provider: registration failed: %s",
            exc,
        )
