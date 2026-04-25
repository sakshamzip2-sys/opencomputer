"""PR-C: TransportBase ABC + NormalizedRequest/Response shape tests."""
from __future__ import annotations

import dataclasses

import pytest

from plugin_sdk.core import Message
from plugin_sdk.transports import (
    NormalizedRequest,
    NormalizedResponse,
    TransportBase,
)


def test_normalized_request_is_frozen():
    req = NormalizedRequest(model="m", messages=[Message(role="user", content="hi")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.model = "other"


def test_normalized_request_has_sensible_defaults():
    req = NormalizedRequest(model="m", messages=[])
    assert req.system == ""
    assert req.tools == ()
    assert req.max_tokens == 4096
    assert req.temperature == 1.0
    assert req.stream is False


def test_transport_base_is_abstract():
    """TransportBase cannot be instantiated."""
    with pytest.raises(TypeError):
        TransportBase()


def test_transport_base_subclass_must_implement_methods():
    """A subclass missing abstract methods can't be instantiated."""

    class IncompleteTransport(TransportBase):
        name = "incomplete"
        # Missing: format_request, send, send_stream, parse_response

    with pytest.raises(TypeError):
        IncompleteTransport()


def test_transport_base_complete_subclass_works():
    """A subclass implementing all abstract methods can be instantiated."""

    class CompleteTransport(TransportBase):
        name = "complete"

        def format_request(self, req):
            return {}

        async def send(self, native):
            return None

        async def send_stream(self, native):
            yield None

        def parse_response(self, raw):
            return None

    t = CompleteTransport()
    assert t.name == "complete"


def test_plugin_sdk_exports_transport_types():
    """Top-level plugin_sdk import surface includes Transport types."""
    import plugin_sdk

    assert hasattr(plugin_sdk, "TransportBase")
    assert hasattr(plugin_sdk, "NormalizedRequest")
    assert hasattr(plugin_sdk, "NormalizedResponse")
