"""Public run_recipe API end-to-end with mock fetcher."""
import pytest
import yaml


def test_run_recipe_end_to_end(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "demo.yaml").write_text(yaml.safe_dump({
        "name": "demo",
        "commands": {
            "list": {
                "pipeline": [
                    {"fetch": "https://example.com/{{ topic }}.json"},
                    {"take": "{{ limit | default(2) }}"},
                ],
            },
        },
    }))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    from opencomputer.recipes import run_recipe

    captured_urls = []

    def fake_fetcher(url):
        captured_urls.append(url)
        return ["a", "b", "c", "d"]

    out = run_recipe(
        site="demo",
        verb="list",
        args={"topic": "things", "limit": 2},
        fetcher=fake_fetcher,
        fmt="json",
    )

    assert captured_urls == ["https://example.com/things.json"]
    assert "a" in out and "b" in out and "c" not in out  # take=2


def test_run_recipe_unknown_verb_raises(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "demo.yaml").write_text(yaml.safe_dump({
        "name": "demo",
        "commands": {"list": {"pipeline": [{"fetch": "https://x"}]}},
    }))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_BUNDLED_DIR", str(bundled))
    monkeypatch.setenv("OPENCOMPUTER_RECIPES_PROFILE_DIR", str(tmp_path / "profile"))

    from opencomputer.recipes import run_recipe

    with pytest.raises(KeyError):
        run_recipe(site="demo", verb="bogus", args={}, fetcher=lambda u: [])
