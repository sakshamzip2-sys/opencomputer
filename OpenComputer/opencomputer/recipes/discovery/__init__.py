"""Discovery layer: explore / cascade / synthesize / generate.

cascade: pure HTTP probing — no API key needed. Tries PUBLIC, COOKIE
(via OPENCOMPUTER_BROWSER_CDP_URL session), HEADER strategies in
order, returns the first that yields a 2xx.

explore: launches a Playwright session (CDP attach if configured) and
records every URL the site fetches into endpoints.json. No LLM-side
capability inference — that's synthesize's job.

synthesize: needs an LLM API key. STUB until ANTHROPIC_API_KEY or
OPENAI_API_KEY is wired into the routine.

generate: explore + synthesize composed. STUB inherits the synthesize
key requirement.
"""

from opencomputer.recipes.discovery.cascade import (
    CascadeResult,
    run_cascade,
)
from opencomputer.recipes.discovery.explorer import explore_endpoints

__all__ = ["CascadeResult", "explore_endpoints", "run_cascade"]
