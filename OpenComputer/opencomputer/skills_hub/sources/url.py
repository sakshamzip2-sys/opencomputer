"""UrlSource — install a single SKILL.md from any HTTP(S) URL.

Wave 5 T18 — port hermes-agent ``9c416e20a``. Plugs into the existing
:class:`opencomputer.skills_hub.router.SkillSourceRouter` which routes by
``<source>/<name>`` identifier prefix. Since URLs themselves contain ``/``,
the URL is urlsafe-base64-encoded into the identifier slug:

    url/aHR0cHM6Ly9leGFtcGxlLmNvbS9za2lsbC5tZA  →  https://example.com/skill.md

Search returns ``[]`` (URL skills are install-by-direct-identifier only;
they don't appear in keyword search). ``inspect`` and ``fetch`` HTTP-GET
the URL once and parse the YAML frontmatter for name + description.

URLs under ``/.well-known/skills/`` are intentionally NOT claimed here —
those route through :class:`opencomputer.skills_hub.sources.well_known.WellKnownSource`.

Trust level: ``community`` — the security scan still runs at install time.
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urlparse

import httpx
import yaml

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

_log = logging.getLogger(__name__)

_WELL_KNOWN_PATH: str = "/.well-known/skills/"


def encode_url(url: str) -> str:
    """Encode an http(s) URL to a router-safe identifier slug.

    Padding ``=`` is stripped (added back at decode time). The result
    contains only ``[A-Za-z0-9_-]`` so the router's ``<source>/<name>``
    prefix split survives.
    """
    return (
        base64.urlsafe_b64encode(url.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_slug(slug: str) -> str:
    """Inverse of :func:`encode_url`. Re-pads the base64 before decode."""
    pad = "=" * (-len(slug) % 4)
    return base64.urlsafe_b64decode(slug + pad).decode("utf-8")


class UrlSource(SkillSource):
    """SkillSource adapter for direct-URL SKILL.md installs."""

    @property
    def name(self) -> str:
        return "url"

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        # URL skills are install-by-identifier; never surface in search.
        return []

    def fetch(self, identifier: str) -> SkillBundle | None:
        url = self._url_from_identifier(identifier)
        if url is None:
            return None
        try:
            text = self._http_get(url)
        except Exception as exc:  # noqa: BLE001 — log + return None per ABC contract
            _log.warning("UrlSource fetch failed for %s: %s", url, exc)
            return None
        return SkillBundle(identifier=identifier, skill_md=text, files={})

    def inspect(self, identifier: str) -> SkillMeta | None:
        url = self._url_from_identifier(identifier)
        if url is None:
            return None
        try:
            text = self._http_get(url)
        except Exception as exc:  # noqa: BLE001
            _log.warning("UrlSource inspect failed for %s: %s", url, exc)
            return None
        fm, _ = _split_frontmatter(text)
        name = (fm or {}).get("name") or _slug_from_url(url)
        description = (fm or {}).get("description", "")
        return SkillMeta(
            identifier=identifier,
            name=name,
            description=description,
            source=self.name,
            trust_level="community",
        )

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _url_from_identifier(self, identifier: str) -> str | None:
        """Return the underlying URL for a ``url/<slug>`` identifier, else None."""
        if not identifier.startswith("url/"):
            return None
        slug = identifier[len("url/"):]
        try:
            url = decode_slug(slug)
        except Exception:
            return None
        if not url.startswith(("http://", "https://")):
            return None
        if _WELL_KNOWN_PATH in urlparse(url).path:
            # /.well-known/skills/* is WellKnownSource's domain.
            return None
        if not url.endswith(".md"):
            return None
        return url

    @staticmethod
    def _http_get(url: str) -> str:
        """Synchronous HTTP GET. Used inside fetch/inspect (which the router
        calls from sync context). Times out after 15 seconds.
        """
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Parse leading YAML frontmatter delimited by ``---`` lines.

    Returns ``(None, text)`` if no frontmatter is present or the YAML
    fails to parse. Tolerant by design — the rest of the markdown body
    is always returned even if the metadata is malformed.
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    try:
        fm = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None, text
    body = text[end + 4:].lstrip("\n")
    return (fm if isinstance(fm, dict) else None), body


def _slug_from_url(url: str) -> str:
    """Last path segment minus the .md suffix; falls back to ``unnamed-skill``."""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"\.md$", "", last, flags=re.IGNORECASE)
    return last or "unnamed-skill"
