"""Adapter: arxiv/search — arXiv preprint search (PUBLIC, no browser).

The arXiv export API returns Atom XML. We parse minimally with stdlib —
no extra deps so the bundled pack stays light.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote

from extensions.adapter_runner import Strategy, adapter

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


@adapter(
    site="arxiv",
    name="search",
    description="arXiv preprint search — title/authors/abstract/published/url.",
    domain="arxiv.org",
    strategy=Strategy.PUBLIC,
    browser=False,
    args=[
        {"name": "query", "type": "string", "required": True, "help": "Search query"},
        {"name": "limit", "type": "int", "default": 10, "help": "Max results"},
    ],
    columns=["title", "authors", "published", "abstract", "url"],
)
async def run(args, ctx):
    query = (args.get("query") or "").strip()
    if not query:
        return []
    limit = max(1, int(args.get("limit") or 10))
    url = (
        "http://export.arxiv.org/api/query?search_query="
        f"all:{quote(query)}&start=0&max_results={limit}"
    )
    body = await ctx.fetch(url)
    if not isinstance(body, str):
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    results: list[dict] = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title_el = entry.find("a:title", _ATOM_NS)
        summary_el = entry.find("a:summary", _ATOM_NS)
        published_el = entry.find("a:published", _ATOM_NS)
        link_el = entry.find("a:id", _ATOM_NS)

        authors: list[str] = []
        for a in entry.findall("a:author/a:name", _ATOM_NS):
            if a.text:
                authors.append(a.text.strip())

        results.append(
            {
                "title": (title_el.text or "").strip().replace("\n", " "),
                "authors": ", ".join(authors),
                "published": (published_el.text or "")[:10] if published_el is not None else "",
                "abstract": (summary_el.text or "").strip().replace("\n", " ")[:400]
                if summary_el is not None
                else "",
                "url": (link_el.text or "").strip() if link_el is not None else "",
            }
        )
    return results
