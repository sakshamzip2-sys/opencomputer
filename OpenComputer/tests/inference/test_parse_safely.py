from opencomputer.inference.parse_safely import parse_safely


def test_parse_safely_returns_parsed_dict_on_valid_json():
    result = parse_safely('{"a": 1}', default={})
    assert result == {"a": 1}


def test_parse_safely_returns_default_on_invalid_json():
    result = parse_safely("not json", default={"fallback": True})
    assert result == {"fallback": True}


def test_parse_safely_logs_parse_error(caplog):
    import logging
    caplog.set_level(logging.WARNING)
    parse_safely("bad", default={})
    assert any("parse_safely" in rec.message for rec in caplog.records)
