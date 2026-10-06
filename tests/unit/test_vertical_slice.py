from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.documents import search_document_from_crawl
from search_engine.publisher import load_active_search_service, publish_lexical_generation
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState


def test_crawl_to_published_search_vertical_slice(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/python"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    crawled = CrawlResultProcessor(candidates).process_html(
        url_id, "<title>Python crawler</title><h1>CPU search</h1><p>the python crawler indexes the web</p>"
    )
    publish_lexical_generation(
        tmp_path / "published",
        "g1",
        {"segment-1": (search_document_from_crawl(crawled),)},
    )
    response = load_active_search_service(tmp_path / "published", require_signatures=False).search("python crawler")
    assert response.results[0].hit.title == "Python crawler"
