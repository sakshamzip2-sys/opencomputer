"""Tests for the multi-source SkillSource router."""
from opencomputer.skills_hub.router import SkillSourceRouter
from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource


class _FakeSource(SkillSource):
    def __init__(self, name: str, items: list[dict]) -> None:
        self._name = name
        self._items = items

    @property
    def name(self) -> str:
        return self._name

    def _to_meta(self, item: dict) -> SkillMeta:
        return SkillMeta(
            identifier=f"{self._name}/{item['name']}",
            name=item["name"],
            description=item["description"],
            source=self._name,
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        out = []
        for item in self._items:
            if query == "" or query in item["name"] or query in item["description"]:
                out.append(self._to_meta(item))
            if len(out) >= limit:
                break
        return out

    def fetch(self, identifier: str) -> SkillBundle | None:
        for item in self._items:
            if identifier == f"{self._name}/{item['name']}":
                return SkillBundle(
                    identifier=identifier,
                    skill_md=item.get("skill_md", "---\nname: x\ndescription: a valid description here for testing purposes\n---"),
                    files={},
                )
        return None

    def inspect(self, identifier: str) -> SkillMeta | None:
        for item in self._items:
            if identifier == f"{self._name}/{item['name']}":
                return self._to_meta(item)
        return None


def test_router_search_aggregates_sources():
    a = _FakeSource("a", [{"name": "foo", "description": "from a"}])
    b = _FakeSource("b", [{"name": "foo", "description": "from b"}])
    router = SkillSourceRouter([a, b])
    results = router.search("foo")
    sources = sorted(r.source for r in results)
    assert sources == ["a", "b"]


def test_router_fetch_routes_by_identifier_prefix():
    a = _FakeSource("a", [{"name": "x", "description": "from a", "skill_md": "FROM_A"}])
    b = _FakeSource("b", [{"name": "x", "description": "from b", "skill_md": "FROM_B"}])
    router = SkillSourceRouter([a, b])
    bundle = router.fetch("b/x")
    assert bundle is not None
    assert bundle.skill_md == "FROM_B"


def test_router_fetch_returns_none_for_unknown_source():
    a = _FakeSource("a", [])
    router = SkillSourceRouter([a])
    assert router.fetch("nonexistent/x") is None


def test_router_search_filtered_to_one_source():
    a = _FakeSource("a", [{"name": "foo", "description": "from a"}])
    b = _FakeSource("b", [{"name": "foo", "description": "from b"}])
    router = SkillSourceRouter([a, b])
    results = router.search("foo", source_filter="a")
    assert len(results) == 1
    assert results[0].source == "a"


def test_router_failing_source_does_not_break_others():
    class _BoomSource(SkillSource):
        @property
        def name(self) -> str:
            return "boom"

        def search(self, query, limit=10):
            raise RuntimeError("network down")

        def fetch(self, identifier):
            raise RuntimeError("network down")

        def inspect(self, identifier):
            raise RuntimeError("network down")

    a = _FakeSource("a", [{"name": "foo", "description": "ok"}])
    router = SkillSourceRouter([_BoomSource(), a])
    results = router.search("foo")
    assert len(results) == 1
    assert results[0].source == "a"


def test_router_list_sources():
    a = _FakeSource("a", [])
    b = _FakeSource("b", [])
    router = SkillSourceRouter([a, b])
    assert router.list_sources() == ["a", "b"]


def test_router_inspect_routes_by_prefix():
    a = _FakeSource("a", [{"name": "x", "description": "from a"}])
    b = _FakeSource("b", [{"name": "x", "description": "from b"}])
    router = SkillSourceRouter([a, b])
    meta = router.inspect("b/x")
    assert meta is not None
    assert meta.description == "from b"


def test_router_inspect_unknown_returns_none():
    a = _FakeSource("a", [])
    router = SkillSourceRouter([a])
    assert router.inspect("a/nope") is None


def test_router_search_empty_query_no_filter_returns_all():
    a = _FakeSource("a", [
        {"name": "x", "description": "first"},
        {"name": "y", "description": "second"},
    ])
    router = SkillSourceRouter([a])
    assert len(router.search("", limit=10)) == 2
