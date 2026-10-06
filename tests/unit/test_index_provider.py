from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation


def _document(url: str, title: str, body: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, title, body)


def test_published_service_lazily_loads_only_routed_index_shards(tmp_path: Path) -> None:
    publish_lexical_generation(
        tmp_path,
        "g1",
        {
            "python": (_document("https://example.com/python", "Python", "python crawler"),),
            "other": (_document("https://other.example/", "Other", "other topic"),),
        },
    )
    service = load_active_search_service(tmp_path, require_signatures=False, max_active_shards=1, max_active_bytes=10_000)
    assert service.stats()["loaded_shards"] == 0
    assert service.search("python").results[0].hit.title == "Python"
    assert service.stats()["loaded_shards"] == 1
