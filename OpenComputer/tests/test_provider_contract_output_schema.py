from plugin_sdk.provider_contract import BaseProvider


def test_baseprovider_complete_accepts_output_schema_kwarg():
    """Backward compat: subclasses without output_schema must still work."""
    import inspect
    sig = inspect.signature(BaseProvider.complete)
    assert "output_schema" in sig.parameters
    assert sig.parameters["output_schema"].default is None


def test_baseprovider_stream_complete_accepts_output_schema_kwarg():
    import inspect
    sig = inspect.signature(BaseProvider.stream_complete)
    assert "output_schema" in sig.parameters
    assert sig.parameters["output_schema"].default is None
