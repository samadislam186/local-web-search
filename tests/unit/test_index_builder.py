from pathlib import Path

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.index_builder import LocalIndexBuilder
from search_engine.publisher import load_active_search_service
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState
from search_engine.tantivy_adapter import tantivy_capability


@pytest.mark.parametrize("backend", ("reference", "tantivy"))
def test_local_index_builder_publishes_pending_crawl_documents(tmp_path: Path, backend: str) -> None:
    if backend == "tantivy" and not tantivy_capability().available:
        pytest.skip("Tantivy is not installed")
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/python"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    crawled = CrawlResultProcessor(candidates).process_html(
        url_id, "<title>Python crawler</title><p>Build a local search index.</p><a href='/rust'>Rust crawler</a>"
    )
    documents = CrawledDocumentRepository(database)
    documents.save(crawled)
    result = LocalIndexBuilder(documents, candidates).build(tmp_path / "artifacts", "g000001", max_documents=10, backend=backend)
    service = load_active_search_service(tmp_path / "artifacts", require_signatures=False)
    assert (result.document_count, result.shard_count) == (1, 1)
    assert service.search("python crawler").results[0].hit.url == "https://example.com/python"
    assert candidates.get_state(url_id) is UrlState.INDEXED
    assert documents.pending_for_index(limit=10) == ()
    second_id = candidates.upsert(canonicalize_url("https://example.com/rust"), "seed")
    candidates.transition(second_id, UrlState.LIVENESS_PENDING)
    candidates.transition(second_id, UrlState.LIVE)
    second = CrawlResultProcessor(candidates).process_html(
        second_id, "<title>Rust crawler</title><p>Build another local search index.</p>"
    )
    documents.save(second)
    with pytest.raises(ValueError, match="segment_source_bytes"):
        LocalIndexBuilder(documents, candidates).build(
            tmp_path / "artifacts", "rejected", max_documents=10, backend=backend, segment_source_bytes=1,
        )
    assert len(documents.pending_for_index(limit=10)) == 1
    assert candidates.get_state(second_id) is UrlState.CRAWLED
    assert load_active_search_service(tmp_path / "artifacts", require_signatures=False).search("python").results
    rebuilt = LocalIndexBuilder(documents, candidates).build(
        tmp_path / "artifacts", "g000002", max_documents=10, backend=backend, segment_documents=1,
    )
    reloaded = load_active_search_service(tmp_path / "artifacts", require_signatures=False)
    assert rebuilt.document_count == 2
    assert rebuilt.shard_count == 2
    assert rebuilt.reused_shards == 1
    assert rebuilt.rebuilt_shards == 1
    assert reloaded.search("python crawler").results[0].hit.url == "https://example.com/python"
    assert reloaded.search("rust crawler").results[0].hit.url == "https://example.com/rust"
    assert reloaded.signals[second_id.hex].authority > reloaded.signals[url_id.hex].authority
    candidates.transition(url_id, UrlState.CRAWLABLE)
    documents.save(CrawlResultProcessor(candidates).process_html(
        url_id, "<title>Replacement document</title><p>Entirely updated content.</p>",
    ))
    third = LocalIndexBuilder(documents, candidates).build(
        tmp_path / "artifacts", "g000003", max_documents=1, backend=backend, segment_documents=1,
    )
    assert third.document_count == 2
    assert third.reused_shards == 1
    assert third.rebuilt_shards == 1
    latest = load_active_search_service(tmp_path / "artifacts", require_signatures=False)
    assert latest.search("replacement").results[0].hit.url_id == url_id
    assert latest.search("python").results[0].hit.title == "Replacement document"
    assert latest.search("rust").results[0].hit.url_id == second_id
    assert documents.pending_for_index(limit=10) == ()
    assert not list((tmp_path / "artifacts").glob(".rebuild-*"))


def test_rebuild_checkpoints_do_not_drop_concurrent_recrawls(tmp_path, monkeypatch) -> None:
    import search_engine.engine.index_builder as builder_module

    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/python"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    processor = CrawlResultProcessor(candidates)
    documents = CrawledDocumentRepository(database)
    documents.save(processor.process_html(url_id, "<title>Original</title><p>Old document.</p>"))
    original_publish = builder_module.publish_lexical_generation

    def publish_then_recrawl(*args, **kwargs):
        manifest = original_publish(*args, **kwargs)
        documents.save(processor.process_html(url_id, "<title>Replacement</title><p>New document.</p>"))
        return manifest

    monkeypatch.setattr(builder_module, "publish_lexical_generation", publish_then_recrawl)
    LocalIndexBuilder(documents, candidates).build(tmp_path / "artifacts", "g1", max_documents=1)
    assert documents.pending_for_index(limit=1)[0].title == "Replacement"
    assert candidates.get_state(url_id) is UrlState.CRAWLED
    assert load_active_search_service(tmp_path / "artifacts", require_signatures=False).search("original").results


def test_disk_staging_replaces_documents_and_bounds_segments(tmp_path: Path) -> None:
    import sqlite3

    from search_engine.engine.identity import UrlId
    from search_engine.engine.index import SearchDocument
    from search_engine.engine.index_builder import _stage_document, _staged_segments

    with sqlite3.connect(":memory:") as snapshot:
        snapshot.execute("CREATE TABLE documents(id BLOB PRIMARY KEY, partition INTEGER, payload TEXT)")
        for number in range(17):
            url = f"https://example.com/{number}"
            _stage_document(snapshot, SearchDocument(UrlId.from_canonical_url(canonicalize_url(url)), url, "old", "text"), 2)
        url = "https://example.com/0"
        _stage_document(snapshot, SearchDocument(UrlId.from_canonical_url(canonicalize_url(url)), url, "updated", "new"), 2)
        segments = list(_staged_segments(snapshot, 3, 1024))
        assert all(len(documents) <= 3 for _, documents in segments)
        assert len({name for name, _ in segments}) == len(segments)
        documents = [document for _, items in segments for document in items]
        assert len(documents) == 17
        assert next(document for document in documents if document.url == url).title == "updated"
        assert list(_staged_segments(snapshot, 3, 1024)) == segments
        with pytest.raises(ValueError, match="segment_source_bytes"):
            list(_staged_segments(snapshot, 3, 1))


def test_rebuild_peak_memory_is_smaller_than_previous_corpus(tmp_path, monkeypatch, record_property) -> None:
    import tracemalloc

    from search_engine.identity import UrlId
    from search_engine.index import SearchDocument
    from search_engine.publisher import publish_lexical_generation

    root = tmp_path / "artifacts"
    body = "bounded corpus text " * 2048

    def segments():
        for shard in range(40):
            items = []
            for number in range(shard * 10, shard * 10 + 10):
                url = canonicalize_url(f"https://example.com/document-{number}")
                items.append(SearchDocument(UrlId.from_canonical_url(url), url.value, "Previous document", body))
            yield f"old-{shard}", tuple(items)

    previous = publish_lexical_generation(root, "previous", segments())
    previous_bytes = sum(artifact.header.payload_length for artifact in previous.artifacts
                         if artifact.header.shard_type == "lexical_index")
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/new"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    documents = CrawledDocumentRepository(database)
    documents.save(CrawlResultProcessor(candidates).process_html(url_id, "<title>Additional</title><p>New content.</p>"))

    def forbid_materialized_pending(**kwargs):
        pytest.fail("rebuild must use the streaming pending reader")

    monkeypatch.setattr(documents, "pending_for_index", forbid_materialized_pending)
    tracemalloc.start()
    try:
        result = LocalIndexBuilder(documents, candidates).build(
            root, "updated", max_documents=1, segment_documents=10, segment_source_bytes=1 << 20,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.document_count == 401
    assert result.shard_count == 41
    # Regression guard: Python allocations cannot contain the old corpus even
    # once. This is not a native-RSS or representative-web scale claim.
    assert peak < previous_bytes / 2, (peak, previous_bytes)
    record_property("rebuild_python_peak_bytes", peak)
    record_property("previous_lexical_payload_bytes", previous_bytes)
    assert not list(root.glob(".rebuild-*"))
