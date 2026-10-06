from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import Bm25fIndex, SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation


def _document(url: str, title: str, body: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, title, body)


def test_published_generation_loads_as_read_only_search_service(tmp_path: Path) -> None:
    manifest = publish_lexical_generation(
        tmp_path,
        "g000001",
        {
            "python": (_document("https://example.com/python", "Python crawler", "crawl web pages"),),
            "other": (_document("https://other.example/", "Other", "different content"),),
        },
    )
    response = load_active_search_service(tmp_path, require_signatures=False).search("python")
    assert manifest.generation == "g000001"
    assert response.results[0].hit.url == "https://example.com/python"
    restricted = load_active_search_service(
        tmp_path, require_signatures=False, minimum_routing_score=3.0
    ).search("python")
    assert restricted.results == ()


def test_publisher_splits_serialized_indexes_to_the_configured_payload_target(tmp_path: Path) -> None:
    documents = (
        _document("https://example.com/one", "One", "alpha " * 80),
        _document("https://example.com/two", "Two", "beta " * 80),
    )
    target = max(len(Bm25fIndex((document,)).to_payload()) for document in documents)
    manifest = publish_lexical_generation(
        tmp_path,
        "g000002",
        {"combined": documents},
        target_shard_payload_bytes=target,
    )
    lexical = [artifact for artifact in manifest.artifacts if artifact.header.shard_type == "lexical_index"]
    assert len(lexical) == 2
    assert all(artifact.header.payload_length <= target for artifact in lexical)
    response = load_active_search_service(tmp_path, require_signatures=False).search("alpha beta")
    assert {result.hit.url for result in response.results} == {"https://example.com/one", "https://example.com/two"}
