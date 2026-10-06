from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState


def test_crawled_document_store_supplies_and_marks_index_documents(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/page"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    document = CrawlResultProcessor(candidates).process_html(
        url_id, "<title>Python crawler</title><h1>Indexing</h1><p>Search the web.</p><a href='/next'>next</a>"
    )
    store = CrawledDocumentRepository(database)
    store.save(document)
    pending = store.pending_for_index(limit=10)
    assert pending[0].title == "Python crawler"
    assert pending[0].headings == "Indexing"
    assert pending[0].anchor == "next"
    store.mark_indexed((url_id,), "g000001")
    assert store.pending_for_index(limit=10) == ()


def test_crawled_document_store_finds_bounded_near_duplicates(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    first_id = candidates.upsert(canonicalize_url("https://example.com/one"), "seed")
    second_id = candidates.upsert(canonicalize_url("https://example.com/two"), "seed")
    for url_id in (first_id, second_id):
        candidates.transition(url_id, UrlState.LIVENESS_PENDING)
        candidates.transition(url_id, UrlState.LIVE)
    processor = CrawlResultProcessor(candidates)
    first = processor.process_html(first_id, "<title>Same</title><p>same searchable document</p>")
    second = processor.process_html(second_id, "<title>Same</title><p>same searchable document</p>")
    store = CrawledDocumentRepository(database)
    store.save(first)
    store.save(second)
    matches = store.near_duplicates(second.simhash, max_distance=0, exclude_url_id=second_id)
    assert len(matches) == 1
    match = matches[0]
    assert (match.url_id, match.distance) == (first_id, 0)
    with database.connection() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT url_id, simhash_hex FROM crawled_documents ORDER BY crawled_at DESC, url_id LIMIT 10"
        ).fetchall()
    assert any("crawled_documents_recent" in str(row[3]) for row in plan)
