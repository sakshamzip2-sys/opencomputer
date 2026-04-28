"""Tests for plugin_sdk.format_converters.

Covers markdownv2, slack_mrkdwn, matrix_html, whatsapp_format. Each
converter must be lossless on plain text and never raise on malformed
input (parse-error -> plain-text fallback contract).
"""

from __future__ import annotations

import random

import pytest

from plugin_sdk.format_converters.markdownv2 import (
    convert as to_mdv2,
)
from plugin_sdk.format_converters.markdownv2 import (
    escape_mdv2,
)
from plugin_sdk.format_converters.matrix_html import convert as to_html
from plugin_sdk.format_converters.slack_mrkdwn import convert as to_mrkdwn
from plugin_sdk.format_converters.whatsapp_format import convert as to_whatsapp

# ---------------------------------------------------------------------------
# MarkdownV2
# ---------------------------------------------------------------------------


def test_mdv2_escape_basic_chars():
    s = "1.5 (rate)"
    out = escape_mdv2(s)
    assert out == r"1\.5 \(rate\)"


def test_mdv2_convert_bold():
    assert to_mdv2("**bold**") == "*bold*"


def test_mdv2_convert_italic_star():
    assert to_mdv2("*italic*") == "_italic_"


def test_mdv2_convert_italic_underscore():
    assert to_mdv2("_italic_") == "_italic_"


def test_mdv2_convert_code_fence_preserved():
    src = "```python\nx = 1\n```"
    out = to_mdv2(src)
    assert "```python\nx = 1\n```" in out


def test_mdv2_convert_inline_code_preserved():
    src = "use `x = 1` here"
    out = to_mdv2(src)
    assert "`x = 1`" in out


def test_mdv2_convert_link_format():
    src = "[label](https://example.com)"
    out = to_mdv2(src)
    # URL contents are kept verbatim (after `)` and `\` escaping); label
    # is escaped per MarkdownV2 rules but plain ASCII labels emerge unchanged.
    assert "[label](https://example.com)" in out


def test_mdv2_convert_link_url_paren_handling_does_not_raise():
    # Edge case: nested parens in URL — link regex stops at first `)`, the
    # leftover `)` is escaped as plain text. Behavior is best-effort but
    # MUST NOT raise.
    src = "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"
    out = to_mdv2(src)
    assert isinstance(out, str)
    assert "wiki" in out


def test_mdv2_convert_strikethrough():
    src = "~~strike~~"
    assert to_mdv2(src) == "~strike~"


def test_mdv2_convert_blockquote_chars_escaped():
    # `>` is special in MarkdownV2 — must be escaped when not in a fenced span.
    src = "> quoted"
    out = to_mdv2(src)
    assert "\\>" in out


def test_mdv2_convert_special_chars_escaped_outside_code():
    src = "Hello, world! (1+1=2)"
    out = to_mdv2(src)
    # Telegram MarkdownV2 special chars: _*[]()~`>#+-=|{}.!
    assert "\\!" in out
    assert "\\(" in out


def test_mdv2_empty_input():
    assert to_mdv2("") == ""


def test_mdv2_fallback_does_not_raise():
    # Pathological pattern that may break naive regex flows
    weird = "**unbalanced *italic with ` `code"
    out = to_mdv2(weird)
    assert isinstance(out, str)


# PR-1 review C1 — single-escape inside formatting markers.
# The pipeline previously pre-escaped content inside **bold**/*italic*/~~strike~~/
# # heading substitutions AND re-ran escape_mdv2 over inter-marker chunks in
# step 5, double-escaping every special char. Telegram rendered the doubled
# backslash as a literal, breaking every formatted run that contained
# punctuation. Step 5 now handles all escaping uniformly.


def test_mdv2_special_chars_inside_bold_single_escape():
    assert to_mdv2("**1.5**") == r"*1\.5*"


def test_mdv2_special_chars_inside_italic_single_escape():
    assert to_mdv2("*hello (world)*") == r"_hello \(world\)_"


def test_mdv2_special_chars_inside_heading_single_escape():
    assert to_mdv2("# v1.0").strip() == r"*v1\.0*"


def test_mdv2_special_chars_inside_strike_single_escape():
    assert to_mdv2("~~beta v0.5~~") == r"~beta v0\.5~"


# Amendment B.6 — fuzz test
@pytest.mark.parametrize("seed", range(50))
def test_markdownv2_fuzz_does_not_raise(seed):
    """Random markdown-ish input must not raise from convert()."""
    random.seed(seed)
    chars = "abc123 _*[]()~`>#+-=|{}.!\\\n"
    text = "".join(random.choice(chars) for _ in range(200))
    out = to_mdv2(text)
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Slack mrkdwn
# ---------------------------------------------------------------------------


def test_mrkdwn_link_conversion():
    assert to_mrkdwn("[label](https://example.com)") == "<https://example.com|label>"


def test_mrkdwn_bold_double_to_single():
    assert to_mrkdwn("**bold**") == "*bold*"


def test_mrkdwn_italic_star_to_underscore():
    # Single * is bold in Slack, so * -> _ for italic
    assert to_mrkdwn("*italic*") == "_italic_"


def test_mrkdwn_strike():
    assert to_mrkdwn("~~strike~~") == "~strike~"


def test_mrkdwn_heading_to_bold():
    assert to_mrkdwn("# Heading").strip() == "*Heading*"


def test_mrkdwn_escape_amp_lt_gt():
    assert "&amp;" in to_mrkdwn("foo & bar")
    assert "&lt;" in to_mrkdwn("a < b")
    assert "&gt;" in to_mrkdwn("a > b")


def test_mrkdwn_code_fence_preserved():
    assert "```python" in to_mrkdwn("```python\nx = 1\n```")


def test_mrkdwn_no_double_escape():
    # &amp; should not become &amp;amp;
    assert to_mrkdwn("&amp;") == "&amp;"


# Revisions item 5 — numeric-entity double-escape guard
def test_mrkdwn_no_double_escape_numeric_entity():
    assert to_mrkdwn("&#39;") == "&#39;"


# Spec-reviewer follow-up: hex-form numeric entities (e.g. &#x27;) must not
# get the leading & re-escaped to &amp; either.
def test_mrkdwn_no_double_escape_hex_entity():
    assert to_mrkdwn("&#x27;") == "&#x27;"


def test_mrkdwn_empty_input():
    assert to_mrkdwn("") == ""


# ---------------------------------------------------------------------------
# Matrix HTML
# ---------------------------------------------------------------------------


def test_matrix_html_bold():
    out = to_html("**bold**")
    assert "<strong>bold</strong>" in out


def test_matrix_html_link_safe_scheme():
    out = to_html("[label](https://example.com)")
    assert '<a href="https://example.com">label</a>' in out


def test_matrix_html_link_javascript_rejected():
    out = to_html("[evil](javascript:alert(1))")
    assert "<a" not in out
    assert "evil" in out


def test_matrix_html_link_data_uri_rejected():
    out = to_html("[evil](data:text/html,<script>1</script>)")
    assert "<a " not in out


def test_matrix_html_escape_lt_gt():
    out = to_html("a < b > c")
    assert "&lt;" in out and "&gt;" in out


def test_matrix_html_inline_code_escaped():
    out = to_html("see `<tag>`")
    # `<tag>` characters inside <code> must be HTML-escaped to be safe
    assert "&lt;tag&gt;" in out


def test_matrix_html_empty_input():
    assert to_html("") == ""


# ---------------------------------------------------------------------------
# WhatsApp format
# ---------------------------------------------------------------------------


def test_whatsapp_bold_double_to_single():
    assert to_whatsapp("**bold**") == "*bold*"


def test_whatsapp_bold_underscore_to_single():
    assert to_whatsapp("__bold__") == "*bold*"


def test_whatsapp_strike_double_to_single():
    assert to_whatsapp("~~strike~~") == "~strike~"


def test_whatsapp_heading_to_bold():
    assert to_whatsapp("# Hello").strip() == "*Hello*"


def test_whatsapp_link_inline():
    assert to_whatsapp("[click](https://x.com)") == "click (https://x.com)"


def test_whatsapp_code_fence_preserved():
    src = "```python\nx = 1\n```"
    assert "```python" in to_whatsapp(src)


def test_whatsapp_inline_code_preserved():
    assert "`x`" in to_whatsapp("see `x` here")


def test_whatsapp_empty_input():
    assert to_whatsapp("") == ""
