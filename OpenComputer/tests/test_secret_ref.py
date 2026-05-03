"""SecretRef wire primitive + SecretResolver registry.

Sub-project G (openclaw-parity) Task 8. Opaque secret reference whose
``model_dump()`` cannot accidentally serialize the value.
"""

from __future__ import annotations

import json

from plugin_sdk.wire_primitives import SecretRef, SecretResolver


class TestSecretRef:
    def test_dump_does_not_contain_value(self) -> None:
        ref = SecretRef(ref_id="abc123", hint="anthropic-api-key")
        dumped = ref.model_dump()
        assert dumped == {"$secret_ref": "abc123", "hint": "anthropic-api-key"}
        assert "value" not in dumped
        assert "secret" not in dumped

    def test_json_roundtrip_preserves_ref(self) -> None:
        ref = SecretRef(ref_id="abc123", hint="x")
        s = json.dumps(ref.model_dump())
        loaded = json.loads(s)
        assert loaded == {"$secret_ref": "abc123", "hint": "x"}

    def test_hint_optional_default_empty(self) -> None:
        ref = SecretRef(ref_id="xyz")
        assert ref.hint == ""
        assert ref.model_dump() == {"$secret_ref": "xyz", "hint": ""}

    def test_repr_does_not_leak_value(self) -> None:
        ref = SecretRef(ref_id="abc")
        assert "abc" in repr(ref)
        # SecretRef itself never holds a value, so leak is impossible by construction.
        assert "value=" not in repr(ref)


class TestSecretResolver:
    def test_register_and_resolve(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="sk-real-key", hint="anthropic")
        assert isinstance(ref, SecretRef)
        assert ref.hint == "anthropic"
        assert resolver.resolve(ref) == "sk-real-key"

    def test_resolve_unknown_returns_none(self) -> None:
        resolver = SecretResolver()
        ref = SecretRef(ref_id="never-registered")
        assert resolver.resolve(ref) is None

    def test_register_returns_unique_ref_ids(self) -> None:
        resolver = SecretResolver()
        r1 = resolver.register(value="v1")
        r2 = resolver.register(value="v2")
        assert r1.ref_id != r2.ref_id

    def test_resolve_by_ref_id_string(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="x")
        assert resolver.resolve_by_id(ref.ref_id) == "x"

    def test_resolvers_are_isolated(self) -> None:
        r1 = SecretResolver()
        r2 = SecretResolver()
        ref = r1.register(value="only-in-r1")
        assert r1.resolve(ref) == "only-in-r1"
        assert r2.resolve(ref) is None

    def test_clear_purges_state(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="x")
        assert resolver.resolve(ref) == "x"
        resolver.clear()
        assert resolver.resolve(ref) is None
