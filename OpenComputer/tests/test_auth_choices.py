"""SetupProvider.auth_choices — manifest-declared rich auth UI metadata.

Sub-project G (openclaw-parity) Task 3. Optional alongside the legacy
``auth_methods: list[str]``; wizard prefers ``auth_choices`` when set.
"""

from __future__ import annotations

from opencomputer.plugins.manifest_validator import validate_manifest


def _manifest_with_provider(**provider_overrides: object) -> dict[str, object]:
    provider: dict[str, object] = {
        "id": "anthropic",
        "auth_methods": ["api_key"],
        "env_vars": ["ANTHROPIC_API_KEY"],
    }
    provider.update(provider_overrides)
    return {
        "id": "test",
        "name": "Test",
        "version": "0.1.0",
        "entry": "plugin",
        "kind": "provider",
        "setup": {"providers": [provider]},
    }


class TestAuthChoices:
    def test_default_empty_list(self) -> None:
        schema, err = validate_manifest(_manifest_with_provider())
        assert err == ""
        assert schema is not None
        assert schema.setup is not None
        assert schema.setup.providers[0].auth_choices == []

    def test_full_auth_choice_parses(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(
                auth_choices=[
                    {
                        "method": "api_key",
                        "label": "Anthropic API key",
                        "cli_flag": "--anthropic-key",
                        "option_key": "anthropic.api_key",
                        "group": "anthropic-auth",
                        "onboarding_priority": 100,
                    }
                ]
            )
        )
        assert err == ""
        assert schema is not None
        assert schema.setup is not None
        assert len(schema.setup.providers[0].auth_choices) == 1
        ac = schema.setup.providers[0].auth_choices[0]
        assert ac.method == "api_key"
        assert ac.label == "Anthropic API key"
        assert ac.cli_flag == "--anthropic-key"
        assert ac.option_key == "anthropic.api_key"
        assert ac.group == "anthropic-auth"
        assert ac.onboarding_priority == 100

    def test_method_required(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(auth_choices=[{"label": "X"}])
        )
        assert schema is None
        assert "method" in err

    def test_unknown_field_rejected(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(
                auth_choices=[{"method": "api_key", "label": "X", "garbage": "yes"}]
            )
        )
        assert schema is None

    def test_discovery_propagates_auth_choices_to_dataclass(self, tmp_path) -> None:
        # End-to-end: write manifest to disk + parse via discovery.
        from pathlib import Path

        from opencomputer.plugins.discovery import _parse_manifest

        plugin_dir = tmp_path / "ext"
        plugin_dir.mkdir()
        manifest_path = plugin_dir / "plugin.json"
        import json

        manifest_path.write_text(
            json.dumps(
                {
                    "id": "test",
                    "name": "Test",
                    "version": "0.1.0",
                    "entry": "plugin",
                    "kind": "provider",
                    "setup": {
                        "providers": [
                            {
                                "id": "anthropic",
                                "auth_methods": ["api_key"],
                                "env_vars": ["ANTHROPIC_API_KEY"],
                                "auth_choices": [
                                    {
                                        "method": "api_key",
                                        "label": "Anthropic API key",
                                        "cli_flag": "--anthropic-key",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        m = _parse_manifest(manifest_path)
        assert m is not None
        assert m.setup is not None
        assert len(m.setup.providers[0].auth_choices) == 1
        ac = m.setup.providers[0].auth_choices[0]
        assert ac.method == "api_key"
        assert ac.cli_flag == "--anthropic-key"
