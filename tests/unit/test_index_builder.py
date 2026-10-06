from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.index_builder import LocalIndexBuilder
from search_engine.publisher import load_active_search_service
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState


def test_local_index_builder_publishes_pending_crawl_documents(tmp_path: Path) -> None:
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
    result = LocalIndexBuilder(documents, candidates).build(tmp_path / "artifacts", "g000001", max_documents=10)
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
    rebuilt = LocalIndexBuilder(documents, candidates).build(tmp_path / "artifacts", "g000002", max_documents=10)
    reloaded = load_active_search_service(tmp_path / "artifacts", require_signatures=False)
    assert rebuilt.document_count == 2
    assert reloaded.search("python crawler").results[0].hit.url == "https://example.com/python"
    assert reloaded.search("rust crawler").results[0].hit.url == "https://example.com/rust"
    assert reloaded.signals[second_id.hex].authority > reloaded.signals[url_id.hex].authority
