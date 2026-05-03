"""Tests for client/form_fields.py — normalization shape only.

Critical fix: NO array coercion (the first-pass docstring claim was
wrong; multi-select is handled by the ``select`` action variant, not
``fill``).
"""

from __future__ import annotations

from extensions.browser_control.client.form_fields import normalize_form_field


def test_basic_normalize():
    out = normalize_form_field({"ref": "e12", "type": "text", "value": "hello"})
    assert out == {"ref": "e12", "type": "text", "value": "hello"}


def test_default_type_text():
    out = normalize_form_field({"ref": "e12", "value": "hi"})
    assert out["type"] == "text"


def test_drops_value_if_non_scalar():
    out = normalize_form_field({"ref": "e12", "value": {"oops": "object"}})
    assert "value" not in out


def test_drops_record_with_blank_ref():
    assert normalize_form_field({"ref": "", "value": "x"}) is None
    assert normalize_form_field({"ref": "   ", "value": "x"}) is None
    assert normalize_form_field({}) is None


def test_no_array_coercion():
    """Multi-select is the 'select' variant; 'fill' MUST NOT coerce."""
    out = normalize_form_field({"ref": "e12", "value": "single"})
    assert out["value"] == "single"
    assert not isinstance(out["value"], list)


def test_int_and_bool_pass_through():
    assert normalize_form_field({"ref": "e", "value": 42})["value"] == 42
    assert normalize_form_field({"ref": "e", "value": True})["value"] is True


def test_non_dict_input():
    assert normalize_form_field("not a dict") is None  # type: ignore[arg-type]
