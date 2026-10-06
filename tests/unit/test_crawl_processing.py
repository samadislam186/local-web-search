from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState


def test_crawl_result_processor_extracts_document_and_discovers_links(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    source = candidates.upsert(canonicalize_url("https://example.com/page"), "seed")
    candidates.transition(source, UrlState.LIVENESS_PENDING)
    candidates.transition(source, UrlState.LIVE)
    document = CrawlResultProcessor(candidates).process_html(
        source,
        "<title>Python search</title><h1>Search</h1><p>The python crawler and the index.</p><a href='/next'>next</a>",
    )
    assert document.title == "Python search"
    assert document.language.language == "en"
    assert document.discovered_url_ids
    assert candidates.get_state(source) is UrlState.CRAWLED
    with database.connection() as connection:
        assert connection.execute("SELECT source_record_id FROM candidate_provenance WHERE source='crawled_link'").fetchone()[0] == "https://example.com/page"
