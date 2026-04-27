"""Security primitives for OpenComputer (Phase 3.G).

This package houses defenses applied to content that crosses the
boundary between untrusted external sources and the main LLM. The
flagship primitive is the **prompt-injection instruction detector**:
a conservative classifier that flags content which looks like it is
trying to redirect, hijack, or exfiltrate from the model.

Modules
-------

* :mod:`opencomputer.security.instruction_detector` — rule-based
  detector + :class:`DetectionVerdict` + the
  :func:`default_detector` lazy singleton.
* :mod:`opencomputer.security.sanitize` — one-call helper
  :func:`sanitize_external_content` that wires the detector into a
  drop-in sanitizer for tools that fetch external data; quarantines
  + publishes a :class:`HookSignalEvent` to the F2 bus on detection.

Plugins that fetch external content (e.g. the coding-harness OI bridge,
the WebFetch tool) pipe their payloads through
``sanitize_external_content`` before returning them to the LLM.
"""

from __future__ import annotations

from opencomputer.security.instruction_detector import (
    DetectionVerdict,
    InstructionDetector,
    InstructionDetectorConfig,
    default_detector,
)
from opencomputer.security.sanitize import sanitize_external_content

__all__ = [
    "DetectionVerdict",
    "InstructionDetector",
    "InstructionDetectorConfig",
    "default_detector",
    "sanitize_external_content",
]
