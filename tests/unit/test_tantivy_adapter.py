import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.tantivy_adapter import (
    TantivyIndex,
    TantivyUnavailableError,
    require_tantivy,
    tantivy_capability,
)


def test_tantivy_capability_is_explicit_when_optional_dependency_is_absent() -> None:
    capability = tantivy_capability()
    if capability.available:
        pytest.skip("Tantivy is installed in this environment")
    assert capability.reason == "optional dependency 'tantivy' is not installed"
    with pytest.raises(TantivyUnavailableError, match="not installed"):
        require_tantivy()


def test_tantivy_build_open_and_query_round_trip(tmp_path) -> None:
    if not tantivy_capability().available:
        pytest.skip("Tantivy is not installed in this environment")
    canonical = canonicalize_url("https://example.com/python")
    document = SearchDocument(
        UrlId.from_canonical_url(canonical),
        canonical.value,
        "Python crawler",
        "local search",
        host="example.com",
        domain="example.com",
    )
    built = TantivyIndex.build(tmp_path / "tantivy", (document,))
    assert built.search("python", 1)[0].url_id == document.url_id
    reopened = TantivyIndex.open(tmp_path / "tantivy")
    assert reopened.search("crawler", 1)[0].url == canonical.value
    assert reopened.search("example.com", 1)[0].url_id == document.url_id
