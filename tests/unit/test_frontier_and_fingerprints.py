from datetime import UTC, datetime, timedelta
from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.fingerprints import detect_language, hamming_distance, simhash64
from search_engine.robots import RobotsPolicy
from search_engine.state import CandidateRepository, CrawlStateDatabase, FrontierRepository


def test_frontier_leases_only_one_url_per_host(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidate = CandidateRepository(database).upsert(canonicalize_url("https://example.com/a"), "seed")
    frontier = FrontierRepository(database)
    now = "2026-01-01T00:00:00+00:00"
    frontier.enqueue(candidate, "example.com", 1.0, now)
    assert frontier.lease_next("worker-1", now, "2026-01-01T00:01:00+00:00") == candidate
    assert frontier.lease_next("worker-2", now, "2026-01-01T00:01:00+00:00") is None
    frontier.complete(candidate, "worker-1", "2026-01-01T00:00:01+00:00")


def test_robots_and_document_fingerprints_are_deterministic() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    policy = RobotsPolicy("User-agent: *\nDisallow: /private", now, now + timedelta(hours=1))
    assert policy.permits("OpenSearchLocalBot", "https://example.com/public", now)
    assert not policy.permits("OpenSearchLocalBot", "https://example.com/private/a", now)
    policy = RobotsPolicy(
        "User-agent: *\nCrawl-delay: 4\nUser-agent: OpenSearchLocalBot\nCrawl-delay: 1.5",
        now,
        now + timedelta(hours=1),
    )
    assert policy.crawl_delay("OpenSearchLocalBot", now) == 1.5
    assert policy.crawl_delay("OtherBot", now) == 4.0
    assert detect_language("this is the search engine and this is a test").language == "en"
    identical = simhash64("same document words")
    assert hamming_distance(identical, simhash64("same document words")) == 0
