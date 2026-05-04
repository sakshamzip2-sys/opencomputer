"""Tests for web_fetch readability mode."""

from __future__ import annotations

from opencomputer.tools.web_fetch import (
    WebFetchTool,
    _html_to_article,
    _is_likely_article_url,
)

ARTICLE_HTML = """
<!doctype html>
<html><head><title>The Big Article</title></head><body>
  <nav>HOME | ABOUT | CONTACT</nav>
  <header><h1>Site Header</h1></header>
  <main>
    <article>
      <h1>The Big Article</h1>
      <p>This is the actual content the user cares about. It is a long paragraph
      that contains the gist of the post. Readability should keep this. The
      paragraph contains many words to make sure the readability scorer treats
      it as the main content rather than incidental boilerplate. Lorem ipsum
      dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor
      incididunt ut labore et dolore magna aliqua.</p>
      <p>Another paragraph of substantive content. Lorem ipsum dolor sit amet,
      consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore
      et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation
      ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
    </article>
  </main>
  <footer>FOOTERSITEFOOTERSITE | Privacy | Terms</footer>
  <aside><a>Related: ...</a></aside>
</body></html>
"""


def test_readability_extracts_article_body():
    text = _html_to_article(ARTICLE_HTML)
    assert "actual content the user cares about" in text


def test_readability_strips_nav_and_footer():
    text = _html_to_article(ARTICLE_HTML)
    # Footer text uses a unique sentinel that wouldn't appear in the article body
    assert "FOOTERSITEFOOTERSITE" not in text


def test_readability_returns_empty_on_no_article():
    """If readability extraction yields nothing, return empty string (caller falls back)."""
    junk_html = "<html><body></body></html>"
    text = _html_to_article(junk_html)
    assert text == "" or len(text) < 50


def test_is_likely_article_url_news_domain():
    assert _is_likely_article_url("https://www.medium.com/p/abc123") is True
    assert _is_likely_article_url("https://blog.example.com/foo") is True
    assert _is_likely_article_url("https://example.com/article/123") is True
    assert _is_likely_article_url("https://example.com/posts/123") is True


def test_is_likely_article_url_non_article_domain():
    assert _is_likely_article_url("https://github.com/user/repo") is False
    assert _is_likely_article_url("https://api.example.com/v1/users") is False


def test_web_fetch_tool_schema_exposes_mode_param():
    tool = WebFetchTool()
    sch = tool.schema  # property, not method
    props = sch.parameters.get("properties", {})
    assert "mode" in props
    assert props["mode"]["enum"] == ["auto", "full", "readability"]
