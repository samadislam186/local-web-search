import importlib.util
from pathlib import Path

import pytest
from search_engine.state import CrawlStateDatabase


def test_resource_benchmark_modifies_only_a_database_copy(tmp_path):
    pytest.importorskip("psutil")
    path = Path(__file__).resolve().parents[2] / "scripts/rebuild_benchmark.py"
    specification = importlib.util.spec_from_file_location("rebuild_benchmark", path)
    benchmark = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(benchmark)
    source = CrawlStateDatabase(tmp_path / "source.sqlite3")
    source.initialize()
    benchmark._synthetic(source, 16, 256)
    report = benchmark.run(tmp_path / "measurement", 0, 256, "reference", 1, source.path)
    assert report["corpus"]["kind"] == "provided_database_copy"
    assert len(report["corpus"]["snapshot_blake3"]) == 64
    assert report["phases"][1]["reused_shards"] > 0
    assert report["phases"][2]["reused_shards"] == 0
    assert all(phase["sampled_peak_rss_bytes"] >= phase["baseline_rss_bytes"] for phase in report["phases"])
    with source.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM crawled_documents WHERE indexed_generation IS NULL").fetchone()[0] == 16
        assert connection.execute("SELECT COUNT(*) FROM crawled_documents WHERE body LIKE '%incrementalcheckpoint%'").fetchone()[0] == 0


def test_native_catalog_compatibility_triggers_rebuild(tmp_path, monkeypatch):
    from search_engine.canonicalization import canonicalize_url
    from search_engine.crawl_processing import CrawlResultProcessor
    from search_engine.crawl_store import CrawledDocumentRepository
    from search_engine.state import CandidateRepository, UrlState
    from search_engine.tantivy_adapter import tantivy_capability

    import search_engine.engine.index_builder as builder_module
    from search_engine.engine.index_builder import LocalIndexBuilder

    if not tantivy_capability().available:
        pytest.skip("Tantivy is not installed")
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    documents = CrawledDocumentRepository(database)
    processor = CrawlResultProcessor(candidates)
    builder = LocalIndexBuilder(documents, candidates)

    def add(url):
        identifier = candidates.upsert(canonicalize_url(url), "seed")
        candidates.transition(identifier, UrlState.LIVENESS_PENDING)
        candidates.transition(identifier, UrlState.LIVE)
        documents.save(processor.process_html(identifier, "<title>Native</title><p>Document.</p>"))

    add("https://example.com/old")
    builder.build(tmp_path / "artifacts", "first", max_documents=1, backend="tantivy")
    add("https://example.com/new")
    original = builder_module.load_segment_catalog

    def incompatible(segment):
        routing, identifiers, _version = original(segment)
        return routing, identifiers, "obsolete-binding"

    monkeypatch.setattr(builder_module, "load_segment_catalog", incompatible)
    result = builder.build(tmp_path / "artifacts", "next", max_documents=1, backend="tantivy")
    assert result.document_count == 2
    assert result.reused_shards == 0
