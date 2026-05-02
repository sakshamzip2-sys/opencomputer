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


# ─── Item 1 (2026-05-02): apply_full_cache_control with tools array ─


def test_apply_full_cache_control_with_tools_marks_last_tool_and_3_message_breakpoints():
    """With tools: 1 tools[-1] + 1 system + 2 last non-system msgs = 4 total."""
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    tools = [
        {"name": "Read", "description": "...", "input_schema": {}},
        {"name": "Write", "description": "...", "input_schema": {}},
        {"name": "Bash", "description": "...", "input_schema": {}},
    ]
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]
    out_msgs, out_tools = apply_full_cache_control(msgs, tools)

    assert "cache_control" not in out_tools[0]
    assert "cache_control" not in out_tools[1]
    assert out_tools[2]["cache_control"] == {"type": "ephemeral"}

    msg_breakpoints = 0
    for m in out_msgs:
        c = m.get("content")
        if isinstance(c, list):
            msg_breakpoints += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            msg_breakpoints += 1
    assert msg_breakpoints == 3

    msg1 = out_msgs[1]["content"]
    if isinstance(msg1, list):
        for blk in msg1:
            if isinstance(blk, dict):
                assert "cache_control" not in blk

    tools_breakpoints = sum(1 for t in out_tools if "cache_control" in t)
    assert msg_breakpoints + tools_breakpoints == 4


def test_apply_full_cache_control_no_tools_uses_4_message_breakpoints():
    """Empty/None tools → 4 breakpoints on messages (system + last 3)."""
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "m4"},
    ]
    out_msgs, out_tools = apply_full_cache_control(msgs, [])
    breakpoints = 0
    for m in out_msgs:
        c = m.get("content")
        if isinstance(c, list):
            breakpoints += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            breakpoints += 1
    assert breakpoints == 4
    assert out_tools == []


def test_apply_full_cache_control_does_not_mutate_inputs():
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    tools = [{"name": "Read"}]
    msgs = [{"role": "system", "content": "sys"}]
    apply_full_cache_control(msgs, tools)
    assert "cache_control" not in tools[0]
    assert msgs[0]["content"] == "sys"


def test_apply_full_cache_control_handles_none_tools():
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    msgs = [{"role": "system", "content": "sys"}]
    out_msgs, out_tools = apply_full_cache_control(msgs, None)
    assert out_tools == []
    sys_content = out_msgs[0]["content"]
    if isinstance(sys_content, list):
        assert any("cache_control" in blk for blk in sys_content if isinstance(blk, dict))


def test_apply_full_cache_control_no_system_with_tools_stays_within_4_breakpoints():
    """Edge case: tools present but no system message → must still cap at 4.

    Allocation when no system: 1 tools[-1] + 0 system + up to 3 last messages = 4.
    Verifies the function doesn't accidentally double-budget when system is absent.
    """
    from opencomputer.agent.prompt_caching import apply_full_cache_control

    tools = [
        {"name": "Read", "description": "...", "input_schema": {}},
        {"name": "Bash", "description": "...", "input_schema": {}},
    ]
    # No system message — first message is user, not system
    msgs = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "assistant", "content": "msg4"},
    ]
    out_msgs, out_tools = apply_full_cache_control(msgs, tools)

    # Tools: only last
    tools_breakpoints = sum(1 for t in out_tools if "cache_control" in t)
    assert tools_breakpoints == 1
    assert "cache_control" not in out_tools[0]
    assert out_tools[1]["cache_control"] == {"type": "ephemeral"}

    # Messages: budget = 4 - 1 tools - 0 system = 3 → last 3 of 4 user msgs cached.
    msg_breakpoints = 0
    for m in out_msgs:
        c = m.get("content")
        if isinstance(c, list):
            msg_breakpoints += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            msg_breakpoints += 1
    assert msg_breakpoints == 3

    # Total never exceeds Anthropic's max of 4
    assert tools_breakpoints + msg_breakpoints == 4

    # First message (msg1) should NOT be cached
    msg1_content = out_msgs[0]["content"]
    if isinstance(msg1_content, list):
        for blk in msg1_content:
            if isinstance(blk, dict):
                assert "cache_control" not in blk
