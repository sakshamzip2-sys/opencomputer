"""Phase 4 — grader cost tracking (tokens + USD estimate)."""

from __future__ import annotations

from opencomputer.evals.providers import ProviderShim


class _FakeProvider:
    """Mock async provider returning a response with a usage attribute."""

    async def complete(self, *, model, messages, max_tokens, temperature, site):
        from plugin_sdk.core import Message

        usage = type(
            "U", (), {"input_tokens": 100, "output_tokens": 20}
        )()

        # Build a stand-in response with .message.content and .usage
        return type(
            "ProviderResponse",
            (),
            {
                "message": Message(
                    role="assistant",
                    content="<thinking>ok</thinking><result>correct</result>",
                ),
                "usage": usage,
            },
        )()


def test_provider_shim_returns_usage():
    shim = ProviderShim(_FakeProvider(), model="claude-sonnet-4-6")
    response = shim.complete("test prompt")
    assert response.text.startswith("<thinking>")
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20


class _CountingProvider:
    """Sync grader provider with a configurable response."""

    _model = "claude-opus-4-7"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return type(
            "R",
            (),
            {
                "text": "<result>correct</result>",
                "usage": type(
                    "U", (), {"input_tokens": 50, "output_tokens": 10}
                )(),
            },
        )()


def test_runner_aggregates_grader_cost(tmp_path):
    rubric_dir = tmp_path / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "test_rubric.md").write_text("Was the response correct? Yes.")

    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "_test_rubric_site.jsonl").write_text(
        '{"id": "c1", "input": {"text": "x"}, "rubric_id": "test_rubric"}\n'
        '{"id": "c2", "input": {"text": "y"}, "rubric_id": "test_rubric"}\n'
    )

    # Register a synthetic site for this test
    from opencomputer.evals.runner import run_site
    from opencomputer.evals.sites import SITES
    from opencomputer.evals.types import EvalSite

    SITES["_test_rubric_site"] = EvalSite(
        name="_test_rubric_site",
        # Adapter that returns the input text — anything string-shaped works.
        callable_path="opencomputer.evals.adapters:adapter_instruction_detector",
        grader="rubric",
        rubric_id="test_rubric",
    )
    try:
        provider = _CountingProvider()
        report = run_site(
            site_name="_test_rubric_site",
            cases_dir=cases_dir,
            rubric_dir=rubric_dir,
            grader_provider=provider,
        )
        assert provider.calls == 2
        assert report.input_tokens == 100  # 2 * 50
        assert report.output_tokens == 20  # 2 * 10
        # Opus pricing: 15*100 + 75*20 = 3000 / 1M = 0.003
        assert report.cost_usd is not None
        assert abs(report.cost_usd - 0.003) < 1e-9
    finally:
        del SITES["_test_rubric_site"]


def test_cost_estimate_returns_none_for_unknown_model():
    """Models we don't know list-prices for shouldn't crash; just None."""
    from opencomputer.evals.runner import _estimate_cost

    class _UnknownProvider:
        _model = "some-other-llm"

    cost = _estimate_cost(100, 20, _UnknownProvider())
    assert cost is None


def test_cost_estimate_returns_none_when_zero_tokens():
    from opencomputer.evals.runner import _estimate_cost

    class _Sonnet:
        _model = "claude-sonnet-4-6"

    assert _estimate_cost(0, 0, _Sonnet()) is None
