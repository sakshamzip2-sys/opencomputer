"""Normalize form-field payloads for the ``act`` "fill" variant.

The TS source's first-pass docstring claimed type-coercion to array for
multi-select, but that's wrong — multi-select is handled by the ``select``
variant, not ``fill``. This module is intentionally narrow: validate
``ref``, default ``type`` to ``"text"``, drop ``value`` if not a scalar.
"""

from __future__ import annotations

from typing import Any

DEFAULT_FILL_FIELD_TYPE = "text"


def normalize_form_field(field: dict[str, Any]) -> dict[str, Any] | None:
    """Validate + normalize one ``{ref, type?, value?}`` record.

    Returns ``None`` when ``ref`` is missing/blank — caller should drop
    such records rather than send them to the server.
    """
    if not isinstance(field, dict):
        return None
    ref_raw = field.get("ref")
    ref = ref_raw.strip() if isinstance(ref_raw, str) else ""
    if not ref:
        return None
    type_raw = field.get("type")
    field_type = type_raw.strip() if isinstance(type_raw, str) else ""
    if not field_type:
        field_type = DEFAULT_FILL_FIELD_TYPE
    out: dict[str, Any] = {"ref": ref, "type": field_type}
    value = field.get("value")
    # Note: bool is a subclass of int, but we accept all scalars equally.
    if isinstance(value, (str, int, float, bool)):
        out["value"] = value
    return out


__all__ = ["DEFAULT_FILL_FIELD_TYPE", "normalize_form_field"]
