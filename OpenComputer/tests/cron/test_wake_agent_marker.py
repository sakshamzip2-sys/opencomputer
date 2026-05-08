"""Hermes parity (2026-05-08): wakeAgent JSON marker on agent-path cron output."""

from opencomputer.cron.scheduler import _parse_wake_agent_marker


class TestParseWakeAgentMarker:
    def test_last_line_wake_false_returns_false(self):
        text = 'regular response\n{"wakeAgent": false}\n'
        assert _parse_wake_agent_marker(text) is False

    def test_last_line_wake_true_returns_true(self):
        text = 'response\n{"wakeAgent": true}\n'
        assert _parse_wake_agent_marker(text) is True

    def test_no_marker_returns_default_true(self):
        text = "regular response with no JSON marker\n"
        assert _parse_wake_agent_marker(text) is True

    def test_malformed_json_returns_true(self):
        text = "response\n{not json at all}\n"
        assert _parse_wake_agent_marker(text) is True

    def test_marker_in_middle_ignored(self):
        # Spec: ONLY the last non-empty line is parsed.
        text = '{"wakeAgent": false}\nresponse continues\n'
        assert _parse_wake_agent_marker(text) is True

    def test_empty_string_returns_true(self):
        assert _parse_wake_agent_marker("") is True

    def test_whitespace_only_returns_true(self):
        assert _parse_wake_agent_marker("   \n  \n") is True

    def test_non_dict_json_returns_true(self):
        # JSON valid but not a dict — list, string, number, etc.
        assert _parse_wake_agent_marker('["wakeAgent", false]') is True
        assert _parse_wake_agent_marker("42") is True
        assert _parse_wake_agent_marker('"wakeAgent"') is True

    def test_dict_without_wake_key_returns_true(self):
        text = '{"otherKey": "value"}'
        assert _parse_wake_agent_marker(text) is True

    def test_trailing_blank_lines_skipped(self):
        # Last NON-EMPTY line is what counts.
        text = 'response\n{"wakeAgent": false}\n\n\n'
        assert _parse_wake_agent_marker(text) is False
