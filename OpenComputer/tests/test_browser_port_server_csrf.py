"""Unit tests for the CSRF middleware decision core + ``is_loopback_url``."""

from __future__ import annotations

from extensions.browser_control.server.csrf import (
    is_loopback_url,
    should_reject_browser_mutation,
)

# ─── is_loopback_url ─────────────────────────────────────────────────


def test_is_loopback_127() -> None:
    assert is_loopback_url("http://127.0.0.1:18792")


def test_is_loopback_localhost() -> None:
    assert is_loopback_url("http://localhost:1234")


def test_is_loopback_ipv6() -> None:
    assert is_loopback_url("http://[::1]:80")


def test_is_loopback_127_subnet() -> None:
    assert is_loopback_url("http://127.0.0.7")


def test_is_loopback_rejects_external() -> None:
    assert not is_loopback_url("https://evil.com/")


def test_is_loopback_null_origin_rejected() -> None:
    """``Origin: null`` is treated as not-loopback (sandboxed iframes)."""
    assert not is_loopback_url("null")


def test_is_loopback_empty_string() -> None:
    assert not is_loopback_url("")
    assert not is_loopback_url(None)  # type: ignore[arg-type]


def test_is_loopback_unparseable() -> None:
    assert not is_loopback_url("not a url at all")


# ─── should_reject_browser_mutation ──────────────────────────────────


def test_get_bypasses() -> None:
    assert not should_reject_browser_mutation(
        method="GET", origin="https://evil.com"
    )


def test_options_bypasses_at_module_level_too() -> None:
    # The OPTIONS bypass lives in the middleware __call__; the predicate
    # itself doesn't fast-path OPTIONS — it's not in the mutating set so
    # it returns False anyway.
    assert not should_reject_browser_mutation(method="OPTIONS")


def test_post_with_cross_site_rejected() -> None:
    assert should_reject_browser_mutation(method="POST", sec_fetch_site="cross-site")


def test_post_same_origin_falls_through_to_origin() -> None:
    """``same-origin`` does not authorize; falls through to Origin/Referer
    check."""
    assert should_reject_browser_mutation(
        method="POST",
        origin="https://evil.com",
        sec_fetch_site="same-origin",
    )
    assert not should_reject_browser_mutation(
        method="POST",
        origin="http://127.0.0.1",
        sec_fetch_site="same-origin",
    )


def test_post_origin_loopback_passes() -> None:
    assert not should_reject_browser_mutation(
        method="POST", origin="http://127.0.0.1:18792"
    )


def test_post_origin_external_rejected() -> None:
    assert should_reject_browser_mutation(method="POST", origin="https://evil.com")


def test_post_no_origin_referer_loopback_passes() -> None:
    assert not should_reject_browser_mutation(
        method="POST", referer="http://127.0.0.1/page"
    )


def test_post_no_origin_referer_external_rejected() -> None:
    assert should_reject_browser_mutation(method="POST", referer="https://evil.com/")


def test_post_neither_passes() -> None:
    """Pure curl/Node call (no Origin, no Referer) → pass; auth gates."""
    assert not should_reject_browser_mutation(method="POST")


def test_origin_wins_over_referer() -> None:
    """If Origin is non-empty, Referer is not consulted."""
    # Origin loopback, Referer external → pass.
    assert not should_reject_browser_mutation(
        method="POST",
        origin="http://127.0.0.1",
        referer="https://evil.com",
    )
    # Origin external, Referer loopback → reject.
    assert should_reject_browser_mutation(
        method="POST",
        origin="https://evil.com",
        referer="http://127.0.0.1",
    )


def test_put_patch_delete_also_gated() -> None:
    for m in ("PUT", "PATCH", "DELETE"):
        assert should_reject_browser_mutation(method=m, origin="https://evil.com")


def test_origin_null_is_rejected() -> None:
    """``Origin: null`` from sandboxed iframes is rejected on POST."""
    assert should_reject_browser_mutation(method="POST", origin="null")
