"""JSON parse with typed fallback instead of crash.

Used at the three migrated call sites in Phase 2. When a provider supports
Structured Outputs, parse failures should be impossible. When it doesn't,
this wrapper turns crashes into a typed 'no decision' fallback.
"""

import json
import logging
from typing import Any, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


def parse_safely(raw: str, *, default: T) -> Any | T:
    """Return json.loads(raw) on success; return default on JSONDecodeError.

    Logs the parse error at WARNING level - never silent.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "parse_safely: JSON parse failed (%s). Falling back to default.",
            type(e).__name__,
        )
        return default
