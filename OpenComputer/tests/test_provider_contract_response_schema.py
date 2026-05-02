"""Verify BaseProvider exposes the response_schema parameter for Subsystem C.

Subsystem C (PR #350) added structured-outputs support via the
`response_schema` kwarg. This test guards against accidental signature
removal that would break the eval harness's parse-resilience workflow.
"""
from plugin_sdk.provider_contract import BaseProvider


def test_baseprovider_complete_accepts_response_schema_kwarg():
    """response_schema is the structured-outputs entry point on complete()."""
    import inspect
    sig = inspect.signature(BaseProvider.complete)
    assert "response_schema" in sig.parameters
    assert sig.parameters["response_schema"].default is None


def test_baseprovider_stream_complete_accepts_response_schema_kwarg():
    """Streaming variant must also accept response_schema for symmetry."""
    import inspect
    sig = inspect.signature(BaseProvider.stream_complete)
    assert "response_schema" in sig.parameters
    assert sig.parameters["response_schema"].default is None
