from pathlib import Path

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.evaluation import benchmark_search
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation
from search_engine.storage import ArtifactValidationError, write_shard


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
    report = benchmark_search(service, "python", runs=3)
    assert report.cache_hit_rate == 1.0
    assert service.stats()["cache_hits"] == 3
    assert service.stats()["cache_misses"] == 1
    assert '"report_version": 1' in report.to_json()


def test_benchmark_excludes_warmup_from_cache_and_latency_measurements(tmp_path: Path) -> None:
    document = _document("https://example.com/python", "Python", "crawler")
    publish_lexical_generation(tmp_path, "g1", {"s1": (document,)})
    service = load_active_search_service(tmp_path, require_signatures=False)
    report = benchmark_search(service, "python", runs=3, warmup_runs=1)
    assert report.cache_hit_rate == 1.0
    assert report.benchmark.runs == 3
    assert report.warmup_runs == 1
    assert service.stats()["query_count"] == 4


def test_lazy_reader_rejects_a_shard_replaced_after_startup(tmp_path: Path) -> None:
    document = _document("https://example.com/python", "Python", "crawler")
    publish_lexical_generation(tmp_path, "g1", {"s1": (document,)})
    service = load_active_search_service(tmp_path, require_signatures=False)
    write_shard(
        tmp_path / "generations/g1/indexes/s1.shard", b"replacement",
        shard_type="lexical_index", shard_id="s1", generation="g1",
    )
    with pytest.raises(ArtifactValidationError, match="changed after"):
        service.search("python")
