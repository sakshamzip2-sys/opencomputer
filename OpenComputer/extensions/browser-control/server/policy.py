"""Request policy: which routes a profile is allowed to hit, plus path
normalization for the policy table lookup.

Mirrors OpenClaw's request-policy.ts:

  - ``normalize_browser_request_path(path)`` — trim, ensure leading ``/``,
    strip trailing slashes (keeps root ``/``).
  - ``is_persistent_browser_profile_mutation(method, path)`` — True for
    routes that mutate the *config*: ``POST /profiles/create``,
    ``POST /reset-profile``, ``DELETE /profiles/{name}``.

For the ``existing-session`` profile (capability ``uses_chrome_mcp``),
the upstream gate denies any request returning True from
``is_persistent_browser_profile_mutation`` — the user's Chrome is not
ours to manage.

Path normalization is path-component only — query strings should be
stripped by the caller (the FastAPI ``request.url.path`` does this
already).
"""

from __future__ import annotations

import re

_PROFILES_NAMED_RE = re.compile(r"^/profiles/[^/]+$")


def normalize_browser_request_path(value: str | None) -> str:
    """Trim → ensure leading ``/`` → strip trailing slashes."""
    if value is None:
        return ""
    s = value.strip()
    if not s:
        return s
    if not s.startswith("/"):
        s = "/" + s
    if len(s) > 1:
        while s.endswith("/"):
            s = s[:-1]
            if s == "":
                return "/"
    return s


def is_persistent_browser_profile_mutation(method: str | None, path: str | None) -> bool:
    """True when the route mutates global profile config."""
    if not method or not path:
        return False
    m = method.upper()
    p = normalize_browser_request_path(path)
    if m == "POST" and p == "/profiles/create":
        return True
    if m == "POST" and p == "/reset-profile":
        return True
    return bool(m == "DELETE" and _PROFILES_NAMED_RE.match(p))
