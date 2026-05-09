"""Typed plugin sources + per-source allow/deny policy (v1.1 plan-3 M11.3).

Today (pre-M11.3) ``oc plugin install`` accepts:

- a local directory path (existing)
- a ``git+https://`` / ``git+ssh://`` URL (existing)
- a ``https://`` URL pointing at a tarball (existing)
- a slug resolved via the remote catalog (existing)

This module adds:

- A typed :class:`PluginSource` representing one of five canonical
  source kinds: ``pypi``, ``github``, ``git``, ``directory``, ``url``.
- A canonical parser :func:`parse_source` that turns a user-supplied
  string into a :class:`PluginSource` with the right kind.
- A :class:`PluginSourcePolicy` that loads
  ``~/.opencomputer/<profile>/plugin_sources.yaml`` and enforces
  per-source allow / deny rules so an operator can lock down their
  install posture.

The HTTP-side installers for ``pypi`` (``pip install <name>``) and
``github`` (``gh repo clone owner/repo`` then verify + activate) build
on this module — they call :meth:`PluginSourcePolicy.assert_allowed`
before any network IO, so a deny-list misconfiguration never lets a
disallowed install proceed silently.

YAML shape::

    # ~/.opencomputer/<profile>/plugin_sources.yaml
    sources:
      pypi:
        allow: ["opencomputer-*", "oc-*"]
        deny: ["opencomputer-malware-*"]
      github:
        allow: ["sakshamzip2-sys/*", "anthropics/*"]
      git:
        deny: ["*"]   # disable arbitrary git installs
      url:
        deny: ["*"]   # disable arbitrary URL tarball installs
      directory:
        allow: ["*"]  # local dir installs always allowed by default

If a kind is omitted, default policy applies:
- ``directory`` → allow everything (typical dev workflow)
- ``pypi`` / ``github`` / ``git`` / ``url`` → ``deny *`` (deny-by-default)

This deny-by-default posture is the production-grade choice: an
operator who hasn't written a policy gets no surprise installs from
unverified sources.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("opencomputer.plugins.source_policy")


class PluginSourceKind(Enum):
    """The five canonical plugin install sources."""

    PYPI = "pypi"
    GITHUB = "github"
    GIT = "git"
    DIRECTORY = "directory"
    URL = "url"


@dataclass(frozen=True, slots=True)
class PluginSource:
    """A parsed install request.

    Fields:
        kind:        Canonical source type.
        target:      The identifier to install (pypi name, github
                     ``owner/repo``, full git/url, or directory path).
        ref:         Optional version / branch / tag / commit / path-suffix.
        raw:         The unparsed user-supplied string (audit + logging).
    """

    kind: PluginSourceKind
    target: str
    ref: str | None = None
    raw: str = ""

    def matcher_target(self) -> str:
        """The string used by allow/deny glob matching."""
        if self.kind == PluginSourceKind.PYPI:
            return self.target
        if self.kind == PluginSourceKind.GITHUB:
            return self.target  # "owner/repo"
        if self.kind == PluginSourceKind.GIT:
            return self.target  # full URL
        if self.kind == PluginSourceKind.URL:
            return self.target
        if self.kind == PluginSourceKind.DIRECTORY:
            return self.target
        return self.target


# ─── source parsing ────────────────────────────────────────────────


_PYPI_PREFIX_RE = re.compile(r"^pypi:(?P<name>[A-Za-z0-9._-]+)(?:==(?P<version>.+))?$")
_GITHUB_SHORT_RE = re.compile(
    r"^(?:gh|github):(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:@(?P<ref>[A-Za-z0-9_./-]+))?$"
)
_GITHUB_HTTPS_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:/tree/(?P<ref>[A-Za-z0-9_./-]+))?/?$"
)


def parse_source(raw: str) -> PluginSource:
    """Turn a user-supplied string into a :class:`PluginSource`.

    Resolution order (first match wins):
    1. ``pypi:<name>[==version]``         → PYPI
    2. ``gh:owner/repo[@ref]``            → GITHUB
    3. ``github:owner/repo[@ref]``        → GITHUB (alias)
    4. ``https://github.com/owner/repo``  → GITHUB (with optional /tree/<ref>)
    5. ``git+http://``, ``git+ssh://``,
       ``git+https://``, ``git+file://``  → GIT
    6. ``http(s)://...`` (non-github)     → URL
    7. Anything else                       → DIRECTORY (treated as path)

    Raises ``ValueError`` on empty input.
    """
    if not raw or not raw.strip():
        raise ValueError("empty plugin source")
    s = raw.strip()

    # 1. Explicit pypi: prefix.
    m = _PYPI_PREFIX_RE.match(s)
    if m:
        return PluginSource(
            kind=PluginSourceKind.PYPI,
            target=m.group("name"),
            ref=m.group("version") or None,
            raw=s,
        )

    # 2-3. github short form.
    m = _GITHUB_SHORT_RE.match(s)
    if m:
        return PluginSource(
            kind=PluginSourceKind.GITHUB,
            target=f"{m.group('owner')}/{m.group('repo')}",
            ref=m.group("ref") or None,
            raw=s,
        )

    # 4. github HTTPS URL.
    m = _GITHUB_HTTPS_RE.match(s)
    if m:
        return PluginSource(
            kind=PluginSourceKind.GITHUB,
            target=f"{m.group('owner')}/{m.group('repo')}",
            ref=m.group("ref") or None,
            raw=s,
        )

    # 5. git+... URL.
    if s.startswith(("git+http://", "git+https://", "git+ssh://", "git+file://")):
        return PluginSource(kind=PluginSourceKind.GIT, target=s, raw=s)

    # 6. http(s) URL pointing at a tarball / archive (non-github).
    if s.startswith(("http://", "https://")):
        parsed = urlparse(s)
        if parsed.netloc.lower() == "github.com":
            # Already handled by rule 4; if we fall through here the URL
            # didn't match the strict github pattern (e.g. /releases/).
            # Treat as URL — tarball installer can handle releases assets.
            pass
        return PluginSource(kind=PluginSourceKind.URL, target=s, raw=s)

    # 7. Treat everything else as a directory path.  We don't stat the
    # filesystem here — the caller's installer does that and surfaces
    # a clear error if the path doesn't exist.
    return PluginSource(kind=PluginSourceKind.DIRECTORY, target=s, raw=s)


# ─── policy ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _SourceRules:
    """Allow/deny glob lists for one source kind."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginSourcePolicy:
    """Per-kind allow / deny enforcement.

    Default posture:
    - ``directory`` allows everything (dev convenience).
    - All other kinds deny everything when no policy is loaded.

    Loaded policy overrides this — see :func:`load_policy`.
    """

    rules: dict[PluginSourceKind, _SourceRules] = field(default_factory=dict)

    def is_allowed(self, source: PluginSource) -> bool:
        """Return True if ``source`` passes allow/deny rules.

        Decision logic:
        - If kind has explicit deny rules and the matcher_target hits
          one, return False.
        - If kind has explicit allow rules and the matcher_target hits
          one, return True.
        - If kind has explicit allow rules and matcher_target hits NONE,
          return False (allow-list is a whitelist).
        - If kind has no allow rules and no deny rules:
            - directory → True (default permissive)
            - all others → False (deny-by-default)
        """
        kind_rules = self.rules.get(source.kind, _SourceRules())
        target = source.matcher_target()

        # Explicit deny check first — deny always wins.
        for pattern in kind_rules.deny:
            if fnmatch.fnmatch(target, pattern):
                return False

        # If allow rules are defined, only members of the allow list pass.
        if kind_rules.allow:
            return any(fnmatch.fnmatch(target, p) for p in kind_rules.allow)

        # No allow list → fall back to default-by-kind.
        if source.kind == PluginSourceKind.DIRECTORY:
            return True
        return False

    def assert_allowed(self, source: PluginSource) -> None:
        """Raise :class:`PolicyDeniedError` if not allowed."""
        if not self.is_allowed(source):
            raise PolicyDeniedError(
                f"plugin source not allowed by policy: kind={source.kind.value}, "
                f"target={source.matcher_target()!r}.  Add an allow rule under "
                f"plugin_sources.yaml::sources.{source.kind.value}.allow if you "
                f"trust this source."
            )


class PolicyDeniedError(PermissionError):
    """Raised by :meth:`PluginSourcePolicy.assert_allowed` on disallowed installs."""


# ─── policy loader ─────────────────────────────────────────────────


def load_policy(raw: Any) -> PluginSourcePolicy:
    """Parse the ``plugin_sources.yaml`` body into a policy.

    Accepts ``None`` / empty dict → empty policy (deny-by-default for
    network kinds, allow-everything for directory).

    Strict on type errors so a misconfiguration surfaces at policy
    load time, not at first install attempt.
    """
    if raw is None:
        return PluginSourcePolicy()
    if not isinstance(raw, dict):
        raise ValueError(
            f"plugin_sources.yaml must be a mapping, got {type(raw).__name__}"
        )
    sources_raw = raw.get("sources") or {}
    if not isinstance(sources_raw, dict):
        raise ValueError(
            f"plugin_sources.yaml::sources must be a mapping, "
            f"got {type(sources_raw).__name__}"
        )

    rules: dict[PluginSourceKind, _SourceRules] = {}
    valid_kinds = {k.value for k in PluginSourceKind}
    for kind_str, rule_raw in sources_raw.items():
        if kind_str not in valid_kinds:
            raise ValueError(
                f"plugin_sources.yaml: unknown source kind {kind_str!r}; "
                f"valid: {sorted(valid_kinds)!r}"
            )
        if not isinstance(rule_raw, dict):
            raise ValueError(
                f"plugin_sources.yaml::sources.{kind_str} must be a mapping, "
                f"got {type(rule_raw).__name__}"
            )
        allow = _string_list(rule_raw.get("allow"), where=f"sources.{kind_str}.allow")
        deny = _string_list(rule_raw.get("deny"), where=f"sources.{kind_str}.deny")
        kind = PluginSourceKind(kind_str)
        rules[kind] = _SourceRules(allow=tuple(allow), deny=tuple(deny))

    return PluginSourcePolicy(rules=rules)


def _string_list(value: Any, *, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"plugin_sources.yaml::{where} must be a list, "
            f"got {type(value).__name__}"
        )
    out: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ValueError(
                f"plugin_sources.yaml::{where}[{i}] must be a string, "
                f"got {type(entry).__name__}"
            )
        if not entry.strip():
            raise ValueError(
                f"plugin_sources.yaml::{where}[{i}] must be non-empty"
            )
        out.append(entry.strip())
    return out


__all__ = [
    "PluginSource",
    "PluginSourceKind",
    "PluginSourcePolicy",
    "PolicyDeniedError",
    "load_policy",
    "parse_source",
]
