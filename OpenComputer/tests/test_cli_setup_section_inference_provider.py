"""Tests for the inference-provider wizard section."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_run_lists_all_discovered_providers_plus_custom(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    fake_providers = [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
        {"name": "openai", "label": "OpenAI", "description": "GPT-4"},
    ]
    monkeypatch.setattr(ip, "_discover_providers", lambda: fake_providers)

    captured_choices = []

    def fake_radiolist(question, choices, default=0, description=None, **kw):
        captured_choices.extend(choices)
        return 0  # pick first

    monkeypatch.setattr(ip, "radiolist", fake_radiolist)
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    ip.run_inference_provider_section(ctx)

    labels = [c.label for c in captured_choices]
    assert "Anthropic" in labels and "OpenAI" in labels
    assert "Custom endpoint (enter URL manually)" in labels
    assert "Leave unchanged" in labels


def test_discover_providers_includes_openrouter_manifest_metadata():
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    providers = {p["name"]: p for p in ip._discover_providers()}

    assert "openrouter" in providers
    assert providers["openrouter"]["label"] == "OpenRouter"
    assert providers["openrouter"]["description"] == "100+ models, pay-per-use, free"
    assert providers["openrouter"]["env_var"] == "OPENROUTER_API_KEY"
    assert providers["openrouter"]["default_model"] == "anthropic/claude-opus-4.7"
    assert providers["openrouter"]["signup_url"] == "https://openrouter.ai/keys"


def test_discover_providers_places_openrouter_first():
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    providers = ip._discover_providers()

    assert providers
    assert providers[0]["name"] == "openrouter"


def test_invoke_openrouter_setup_replaces_invalid_old_model(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "openrouter",
            "label": "OpenRouter",
            "description": "100+ models, pay-per-use, free",
            "env_var": "OPENROUTER_API_KEY",
            "signup_url": "https://openrouter.ai/keys",
            "default_model": "anthropic/claude-opus-4.7",
        },
    ])
    monkeypatch.setattr(ip, "_collect_api_key", lambda *a, **kw: None)
    monkeypatch.setattr(ip, "_test_provider_connection", lambda *a, **kw: True)
    monkeypatch.setattr(ip, "_env_value_present", lambda *a, **kw: True)
    monkeypatch.setattr(
        ip,
        "_fetch_openrouter_models",
        lambda *a, **kw: [
            "anthropic/claude-opus-4.7",
            "google/gemma-4-31b-it:free",
            "anthropic/claude-opus-4.6",
        ],
    )
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(
        tmp_path,
        config={"model": {"provider": "anthropic", "model": "claude-opus-4-7"}},
    )
    ok = ip._invoke_provider_setup("openrouter", ctx)

    assert ok is True
    assert ctx.config["model"]["provider"] == "openrouter"
    assert ctx.config["model"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert ctx.config["model"]["model"] == "anthropic/claude-opus-4.7"


def test_openrouter_model_picker_defaults_to_curated_cloud_model(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    captured: dict[str, object] = {}

    def fake_radiolist(question, choices, default=0, **kw):
        captured["question"] = question
        captured["labels"] = [c.label for c in choices]
        captured["default"] = default
        return default

    monkeypatch.setattr(ip, "radiolist", fake_radiolist)
    monkeypatch.setattr(ip, "_fetch_openrouter_models", lambda *a, **kw: [
        "baidu/cobuddy:free",
        "anthropic/claude-opus-4.7",
        "anthropic/claude-opus-4.6",
        "google/gemma-4-31b-it:free",
        "qwen/qwen3-coder:free",
        "openai/gpt-5.1",
    ])

    ctx = _make_ctx(tmp_path, config={"model": {"model": "claude-opus-4-7"}})
    chosen = ip._choose_openrouter_model(
        ctx,
        default_model="anthropic/claude-opus-4.7",
    )

    assert chosen == "anthropic/claude-opus-4.7"
    assert captured["question"] == "Select default OpenRouter model:"
    assert captured["default"] == 0
    labels = captured["labels"]
    assert labels[:4] == [
        "anthropic/claude-opus-4.7",
        "anthropic/claude-opus-4.6",
        "openai/gpt-5.1",
        "qwen/qwen3-coder:free",
    ]
    assert "baidu/cobuddy:free" not in labels
    assert "google/gemma-4-31b-it:free" not in labels
    assert "Enter custom model name" in labels
    assert "Skip (keep current)" in labels


def test_openrouter_curated_defaults_exclude_google_ai_studio_free_models():
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    labels = ip._curate_openrouter_model_ids([
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "qwen/qwen3-coder:free",
        "moonshotai/kimi-k2.6",
        "anthropic/claude-opus-4.7",
    ])

    assert "google/gemma-4-31b-it:free" not in labels
    assert "google/gemma-4-26b-a4b-it:free" not in labels
    assert "qwen/qwen3-coder:free" in labels


def test_openrouter_model_picker_can_skip_keep_current(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    def choose_skip(question, choices, default=0, **kw):
        return next(i for i, choice in enumerate(choices)
                    if choice.value == "__skip__")

    monkeypatch.setattr(ip, "radiolist", choose_skip)
    monkeypatch.setattr(ip, "_fetch_openrouter_models", lambda *a, **kw: [
        "anthropic/claude-opus-4.7",
    ])

    ctx = _make_ctx(tmp_path, config={"model": {"model": "qwen/qwen3-coder:free"}})
    chosen = ip._choose_openrouter_model(
        ctx,
        default_model="anthropic/claude-opus-4.7",
    )

    assert chosen is None


def test_openrouter_model_picker_does_not_default_to_google_current_model(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    captured: dict[str, object] = {}

    def fake_radiolist(question, choices, default=0, **kw):
        captured["labels"] = [c.label for c in choices]
        captured["default"] = default
        return default

    monkeypatch.setattr(ip, "radiolist", fake_radiolist)
    monkeypatch.setattr(ip, "_fetch_openrouter_models", lambda *a, **kw: [
        "anthropic/claude-opus-4.7",
        "qwen/qwen3-coder:free",
    ])

    ctx = _make_ctx(tmp_path, config={"model": {"model": "google/gemma-4-31b-it:free"}})
    chosen = ip._choose_openrouter_model(
        ctx,
        default_model="anthropic/claude-opus-4.7",
    )

    assert chosen == "anthropic/claude-opus-4.7"
    assert captured["default"] == 0
    assert "google/gemma-4-31b-it:free  \u2190 currently in use" not in captured["labels"]


def test_invoke_openai_setup_replaces_incompatible_openrouter_model(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "openai",
            "label": "OpenAI",
            "description": "OpenAI models",
            "env_var": "OPENAI_API_KEY",
            "signup_url": "https://platform.openai.com/api-keys",
            "default_model": "gpt-5.4",
        },
    ])
    monkeypatch.setattr(ip, "_collect_api_key", lambda *a, **kw: None)
    monkeypatch.setattr(ip, "_test_provider_connection", lambda *a, **kw: True)

    ctx = _make_ctx(
        tmp_path,
        config={
            "model": {
                "provider": "openrouter",
                "model": "minimax/minimax-m2.5:free",
            },
        },
    )

    ok = ip._invoke_provider_setup("openai", ctx)

    assert ok is True
    assert ctx.config["model"]["provider"] == "openai"
    assert ctx.config["model"]["model"] == "gpt-5.4"


def test_invoke_openai_setup_keeps_existing_compatible_openai_model(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "openai",
            "label": "OpenAI",
            "description": "OpenAI models",
            "env_var": "OPENAI_API_KEY",
            "signup_url": "https://platform.openai.com/api-keys",
            "default_model": "gpt-5.4",
        },
    ])
    monkeypatch.setattr(ip, "_collect_api_key", lambda *a, **kw: None)
    monkeypatch.setattr(ip, "_test_provider_connection", lambda *a, **kw: True)

    ctx = _make_ctx(
        tmp_path,
        config={"model": {"provider": "openai", "model": "gpt-5.5"}},
    )

    ip._invoke_provider_setup("openai", ctx)

    assert ctx.config["model"]["model"] == "gpt-5.5"


def test_test_provider_connection_loads_plugin_with_plugin_api(monkeypatch):
    from types import SimpleNamespace

    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    class FakeProvider:
        def __init__(self):
            pass

    fake_provider = SimpleNamespace(id="openrouter")
    fake_manifest = SimpleNamespace(
        kind="provider",
        setup=SimpleNamespace(providers=[fake_provider]),
    )
    fake_candidate = SimpleNamespace(manifest=fake_manifest)

    def fake_load_plugin(candidate, api):
        assert candidate is fake_candidate
        assert hasattr(api, "tools")
        api.register_provider("openrouter", FakeProvider)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(
        "opencomputer.plugins.discovery.standard_search_paths",
        lambda: [],
    )
    monkeypatch.setattr(
        "opencomputer.plugins.discovery.discover",
        lambda _paths: [fake_candidate],
    )
    monkeypatch.setattr("opencomputer.plugins.loader.load_plugin", fake_load_plugin)

    assert ip._test_provider_connection("openrouter", "OPENROUTER_API_KEY") is True


def test_run_writes_provider_to_config_on_selection(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 0)  # pick anthropic
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    result = ip.run_inference_provider_section(ctx)

    assert result == SectionResult.CONFIGURED
    # The mocked _invoke_provider_setup doesn't write — the test simply
    # verifies the handler returned CONFIGURED.


def test_run_leave_unchanged_returns_skipped_keep(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    # Choices: [Anthropic, Custom endpoint, Leave unchanged] → idx 2
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 2)

    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    result = ip.run_inference_provider_section(ctx)
    assert result == SectionResult.SKIPPED_KEEP


def test_is_configured_returns_true_when_provider_set(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    assert is_inference_provider_configured(ctx) is True


def test_is_configured_returns_false_for_none_provider(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "none"}})
    assert is_inference_provider_configured(ctx) is False


# ─────────────────────────────────────────────────────────────────
# P — API key entry flow
# ─────────────────────────────────────────────────────────────────


def test_invoke_provider_setup_prompts_and_saves_when_no_existing_key(
    monkeypatch, tmp_path,
):
    """Fresh user picks provider → key prompt → key saved to .env."""
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "anthropic",
            "label": "Anthropic",
            "description": "Claude",
            "env_var": "ANTHROPIC_API_KEY",
            "signup_url": "https://console.anthropic.com/keys",
        },
    ])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(ip, "default_env_file", lambda: env_file)
    monkeypatch.setattr(
        "opencomputer.cli_setup.env_writer.default_env_file",
        lambda: env_file,
    )
    monkeypatch.setattr(ip, "_prompt_api_key",
                         lambda env_var, signup_url="": "sk-ant-newkey")

    ctx = _make_ctx(tmp_path)
    ok = ip._invoke_provider_setup("anthropic", ctx)

    assert ok is True
    assert ctx.config["model"]["provider"] == "anthropic"
    assert ctx.config["model"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert env_file.exists()
    assert "ANTHROPIC_API_KEY=sk-ant-newkey" in env_file.read_text()


def test_invoke_provider_setup_use_existing_key_does_not_overwrite(
    monkeypatch, tmp_path,
):
    """Key already in shell → user picks 'use existing' → no prompt, no .env write."""
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "anthropic", "label": "Anthropic", "description": "",
            "env_var": "ANTHROPIC_API_KEY",
            "signup_url": "https://console.anthropic.com/keys",
        },
    ])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-existing")

    env_file = tmp_path / ".env"
    monkeypatch.setattr(ip, "default_env_file", lambda: env_file)

    # Mock radiolist to choose "Use existing" (idx 0)
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 0)

    prompt_called: list[bool] = []
    monkeypatch.setattr(ip, "_prompt_api_key",
                         lambda *a, **kw: prompt_called.append(True) or "should-not-be-used")

    ctx = _make_ctx(tmp_path)
    ip._invoke_provider_setup("anthropic", ctx)

    assert prompt_called == [], "use-existing must NOT call the prompt"
    assert not env_file.exists(), "use-existing must NOT write to .env"


def test_invoke_provider_setup_re_enter_key_overwrites_dotenv(
    monkeypatch, tmp_path,
):
    """Existing key + user picks 're-enter' → prompt fires + .env updated."""
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "anthropic", "label": "Anthropic", "description": "",
            "env_var": "ANTHROPIC_API_KEY",
            "signup_url": "https://console.anthropic.com/keys",
        },
    ])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-old\n")
    env_file.chmod(0o600)
    monkeypatch.setattr(ip, "default_env_file", lambda: env_file)
    monkeypatch.setattr(
        "opencomputer.cli_setup.env_writer.default_env_file",
        lambda: env_file,
    )

    # idx 1 = "Enter a new key"
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 1)
    monkeypatch.setattr(ip, "_prompt_api_key",
                         lambda env_var, signup_url="": "sk-ant-new")

    ctx = _make_ctx(tmp_path)
    ip._invoke_provider_setup("anthropic", ctx)

    text = env_file.read_text()
    assert "sk-ant-new" in text
    assert "sk-ant-old" not in text


def test_invoke_provider_setup_user_skips_prompt_does_not_write(
    monkeypatch, tmp_path,
):
    """No existing key + user submits empty input → no write, config still updated."""
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "anthropic", "label": "Anthropic", "description": "",
            "env_var": "ANTHROPIC_API_KEY", "signup_url": "",
        },
    ])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(ip, "default_env_file", lambda: env_file)
    # Also patch the env_writer module so read_env_value's internal
    # default_env_file() call doesn't fall through to the real
    # ~/.opencomputer/.env where ANTHROPIC_API_KEY may already be set.
    monkeypatch.setattr(
        "opencomputer.cli_setup.env_writer.default_env_file",
        lambda: env_file,
    )
    monkeypatch.setattr(ip, "_prompt_api_key",
                         lambda env_var, signup_url="": None)

    ctx = _make_ctx(tmp_path)
    ip._invoke_provider_setup("anthropic", ctx)

    assert ctx.config["model"]["provider"] == "anthropic"
    assert ctx.config["model"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert not env_file.exists()


def test_invoke_provider_setup_skip_branch_returns_no_write(
    monkeypatch, tmp_path,
):
    """Existing key + user picks 'Skip' (idx 2) → no prompt + no write."""
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {
            "name": "anthropic", "label": "Anthropic", "description": "",
            "env_var": "ANTHROPIC_API_KEY", "signup_url": "",
        },
    ])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-existing")
    env_file = tmp_path / ".env"
    monkeypatch.setattr(ip, "default_env_file", lambda: env_file)

    # idx 2 = Skip
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 2)

    prompt_called: list[bool] = []
    monkeypatch.setattr(ip, "_prompt_api_key",
                         lambda *a, **kw: prompt_called.append(True) or None)

    ctx = _make_ctx(tmp_path)
    ip._invoke_provider_setup("anthropic", ctx)

    assert prompt_called == []
    assert not env_file.exists()
