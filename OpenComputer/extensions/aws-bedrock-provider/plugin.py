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
    """
    try:
        from extensions.aws_bedrock_provider.provider import BedrockProvider
    except ImportError as exc:
        logger.info(
            "aws-bedrock-provider: boto3 not installed; skipping registration. "
            "Install with `pip install opencomputer[bedrock]`. (%s)",
            exc,
        )
        return

    try:
        provider = BedrockProvider()
        api.register_provider(provider)
        logger.info(
            "aws-bedrock-provider: registered (region=%s)",
            provider._transport._region,
        )
    except Exception as exc:
        logger.warning(
            "aws-bedrock-provider: registration failed (boto3 import worked but "
            "provider init failed — likely AWS credentials not configured): %s",
            exc,
        )
