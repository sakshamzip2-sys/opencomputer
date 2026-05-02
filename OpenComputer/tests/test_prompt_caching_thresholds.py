"""Cache markers must be skipped on blocks below the provider's threshold."""

from opencomputer.agent.prompt_caching import apply_anthropic_cache_control


def _msg(text):
    return {"role": "user", "content": text}


def _has_cache_control(m):
    if m.get("cache_control"):
        return True
    content = m.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and "cache_control" in b for b in content)
    return False


def test_below_threshold_block_skipped():
    """A short block on a 4096-token-min model must not receive cache_control."""
    short = "x" * 200  # ~50 tokens
    long = "y" * (5 * 4096 * 4)  # ~5k tokens, well above threshold
    msgs = [_msg(long), _msg(short)]
    out = apply_anthropic_cache_control(
        msgs,
        native_anthropic=False,
        min_cache_tokens=4096,
    )
    # The short message must NOT carry cache_control.
    assert not _has_cache_control(out[1])
    # The long message SHOULD carry it (walked back from the short tail).
    assert _has_cache_control(out[0])


def test_threshold_zero_marks_everything():
    """Default min=0 preserves today's behaviour: every candidate gets marked."""
    msgs = [_msg("a" * 100), _msg("b" * 100), _msg("c" * 100)]
    out = apply_anthropic_cache_control(msgs, native_anthropic=False)
    found = any(_has_cache_control(m) for m in out)
    assert found


def test_no_eligible_blocks_succeeds_without_markers():
    """If no message clears the threshold, the request proceeds with no markers
    rather than crashing or marking ineligible blocks."""
    msgs = [_msg("x" * 50), _msg("y" * 50), _msg("z" * 50)]
    out = apply_anthropic_cache_control(
        msgs,
        native_anthropic=False,
        min_cache_tokens=10000,
    )
    assert not any(_has_cache_control(m) for m in out)


def test_walks_back_to_find_eligible_block():
    """When the tail is sub-threshold but earlier blocks pass, the marker
    is placed on the most recent eligible block."""
    big = "z" * (5 * 4096 * 4)
    small = "y" * 100
    msgs = [_msg(big), _msg(small), _msg(small)]
    out = apply_anthropic_cache_control(
        msgs,
        native_anthropic=False,
        min_cache_tokens=4096,
    )
    assert _has_cache_control(out[0])
    assert not _has_cache_control(out[1])
    assert not _has_cache_control(out[2])
