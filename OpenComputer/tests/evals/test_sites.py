from opencomputer.evals.sites import SITES, get_site


def test_v1_sites_registered():
    assert "reflect" in SITES
    assert "prompt_evolution" in SITES
    assert "llm_extractor" in SITES
    assert "job_change" in SITES
    assert "instruction_detector" in SITES


def test_get_site_returns_evalsite():
    site = get_site("reflect")
    assert site.name == "reflect"
    assert site.grader == "rubric"


def test_get_site_unknown_raises():
    try:
        get_site("does_not_exist")
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_callable_paths_resolve():
    """Every registered site's callable_path must be importable."""
    import importlib
    for site in SITES.values():
        if not site.requires_provider:
            # regex-based sites still need their callable to exist
            pass
        module_path, _, attr = site.callable_path.partition(":")
        module = importlib.import_module(module_path)
        assert hasattr(module, attr), (
            f"{site.name}: {site.callable_path} not found"
        )
