"""Wave 3 — HuggingFace Inference Providers routing suffix parser.

The wire-format is "suffix passes through verbatim in the model field"
per HF's own docs (HF parses ``meta-llama/...:fastest`` server-side and
auto-routes). The OC client just exposes a recognized-suffix set so:
  - CLI completion can offer the right suffixes
  - Validation can warn on typos
  - Wrappers (``oc model``, future ``oc hf …`` flows) can split + reformat.
"""

from __future__ import annotations

from opencomputer.agent.config import (
    _HF_ROUTING_SUFFIXES,
    _KNOWN_HF_PROVIDERS,
    split_hf_routing_suffix,
)


def test_split_hf_fastest():
    model, suffix = split_hf_routing_suffix(
        "meta-llama/Llama-3.3-70B-Instruct:fastest"
    )
    assert model == "meta-llama/Llama-3.3-70B-Instruct"
    assert suffix == "fastest"


def test_split_hf_cheapest():
    model, suffix = split_hf_routing_suffix(
        "meta-llama/Llama-3.3-70B-Instruct:cheapest"
    )
    assert suffix == "cheapest"


def test_split_hf_specific_provider_groq():
    model, suffix = split_hf_routing_suffix(
        "meta-llama/Llama-3.3-70B-Instruct:groq"
    )
    assert model == "meta-llama/Llama-3.3-70B-Instruct"
    assert suffix == "groq"


def test_split_hf_specific_provider_together():
    _, suffix = split_hf_routing_suffix("Qwen/Qwen-2.5-Coder:together")
    assert suffix == "together"


def test_split_hf_unknown_suffix_passes_through():
    """Unknown suffix (not in _HF_ROUTING_SUFFIXES) is preserved verbatim."""
    model, suffix = split_hf_routing_suffix(
        "meta-llama/Llama-3.3-70B:nonexistent-backend"
    )
    assert model == "meta-llama/Llama-3.3-70B:nonexistent-backend"
    assert suffix is None


def test_split_hf_no_colon_returns_unchanged():
    model, suffix = split_hf_routing_suffix("meta-llama/Llama-3.3-70B-Instruct")
    assert model == "meta-llama/Llama-3.3-70B-Instruct"
    assert suffix is None


def test_known_providers_set_contents():
    """The recognized HF backend set covers the major Inference Providers."""
    expected = {
        "groq", "together", "fireworks", "replicate",
        "sambanova", "hyperbolic", "novita", "cerebras",
    }
    assert expected == _KNOWN_HF_PROVIDERS


def test_routing_suffixes_set_contents():
    assert "fastest" in _HF_ROUTING_SUFFIXES
    assert "cheapest" in _HF_ROUTING_SUFFIXES
    assert "groq" in _HF_ROUTING_SUFFIXES
    assert "novita" in _HF_ROUTING_SUFFIXES
    assert "nonexistent" not in _HF_ROUTING_SUFFIXES
