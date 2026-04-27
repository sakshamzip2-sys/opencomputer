"""V3.B-T1 — Anthropic prompt caching tests."""
from opencomputer.agent.prompt_caching import apply_anthropic_cache_control


def test_empty_messages_returns_empty():
    assert apply_anthropic_cache_control([]) == []


def test_system_message_gets_cache_control():
    msgs = [{"role": "system", "content": "you are an agent"}]
    out = apply_anthropic_cache_control(msgs)
    # System content becomes a list with cache_control on the last text block
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_last_3_non_system_get_cache_control():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "assistant", "content": "msg4"},
    ]
    out = apply_anthropic_cache_control(msgs)
    # 4 breakpoints total: system + last 3 non-system (msg2, msg3, msg4)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4
    # msg1 should NOT have cache_control (only last 3 non-system do)
    msg1_content = out[1]["content"]
    if isinstance(msg1_content, list):
        for blk in msg1_content:
            if isinstance(blk, dict):
                assert "cache_control" not in blk


def test_does_not_mutate_input():
    msgs = [{"role": "system", "content": "sys"}]
    apply_anthropic_cache_control(msgs)
    assert msgs[0]["content"] == "sys"  # untouched


def test_1h_ttl():
    msgs = [{"role": "system", "content": "sys"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="1h")
    cache_block = out[0]["content"][0]
    assert cache_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_native_anthropic_tool_message():
    """When native_anthropic=True, tool messages get cache_control at top level."""
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": "result"}]
    out = apply_anthropic_cache_control(msgs, native_anthropic=True)
    assert "cache_control" in out[0]


def test_max_4_breakpoints_with_many_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    out = apply_anthropic_cache_control(msgs)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4
