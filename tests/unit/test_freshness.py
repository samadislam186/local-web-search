from dataclasses import replace
from datetime import UTC, datetime

from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation


def test_published_index_preserves_and_enforces_crawl_freshness(tmp_path) -> None:
    canonical = canonicalize_url("https://example.com/recent")
    recent = SearchDocument(
        UrlId.from_canonical_url(canonical), canonical.value, "Python", "crawler",
        crawled_at="2026-10-01T00:00:00+00:00",
    )
    old_url = canonicalize_url("https://other.example/old")
    old = replace(recent, url_id=UrlId.from_canonical_url(old_url), url=old_url.value, crawled_at="2020-01-01T00:00:00+00:00")
    publish_lexical_generation(tmp_path, "freshness-g1", {"s1": (recent, old)})
    service = load_active_search_service(tmp_path, require_signatures=False)
    now = datetime(2026, 10, 2, tzinfo=UTC)
    assert len(service.search("python", now=now).results) == 2
    for query, freshness in (("python", "week"), ("python freshness:week", None)):
        response = service.search(query, freshness=freshness, now=now)
        assert [result.hit.url_id for result in response.results] == [recent.url_id]
        assert response.results[0].hit.crawled_at == recent.crawled_at
