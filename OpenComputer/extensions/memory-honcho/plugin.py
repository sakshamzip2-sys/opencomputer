"""OpenComputer plugin: self-hosted Honcho memory provider.

Phase 10f.K — skeleton only. Registers nothing yet. Phase 10f.L wires
the actual ``HonchoSelfHostedProvider`` via ``api.register_memory_provider``.

## Deployment model

- We DO NOT vendor Honcho's source code into this repo. Honcho is
  AGPL-3.0; vendoring would propagate copyleft.
- Instead, the docker-compose bundle (Phase 10f.M) pulls the official
  image from Plastic Labs' registry at install time. Users accept AGPL
  terms by running the pulled container, not by installing our plugin.

## Pinning

The image tag lives in ``IMAGE_VERSION`` next to this file. Update that
file (and run integration tests) before bumping to a new upstream release.
"""

from __future__ import annotations

from typing import Any


def register(api: Any) -> None:
    """Plugin entry point.

    Phase 10f.K — stub. The next phase (10f.L) will:
      1. Load the Honcho config (base URL, cadence, tool toggles).
      2. Instantiate ``HonchoSelfHostedProvider``.
      3. Call ``api.register_memory_provider(provider)``.
    """
    return None
