import sqlite3
from threading import Event

import pytest
from fastapi.testclient import TestClient

from search_engine.api import create_generation_app
from search_engine.engine.cache import BoundedShardCache
from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.crawl_processing import CrawlResultProcessor
from search_engine.engine.crawl_store import CrawledDocumentRepository
from search_engine.engine.crawler_runner import CrawlerRunner
from search_engine.engine.identity import UrlId
from search_engine.engine.index import SearchDocument
from search_engine.engine.publisher import publish_lexical_generation
from search_engine.engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    UrlState,
)
from search_engine.engine.storage import ArtifactValidationError


def test_late_worker_cannot_complete_or_save_a_reissued_lease(tmp_path):
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    identifier = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(identifier, UrlState.LIVENESS_PENDING)
    candidates.transition(identifier, UrlState.LIVE)
    frontier = FrontierRepository(database)
    frontier.enqueue(identifier, "example.com", 1, "2026-01-01T00:00:00+00:00")
    first = frontier.lease_next_details("same-owner", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00")
    second = frontier.lease_next_details("same-owner", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00")
    assert second.attempt_count == first.attempt_count + 1
    document = CrawlResultProcessor(candidates).process_html(identifier, "<p>durable content</p>", transition_state=False)
    documents = CrawledDocumentRepository(database)
    with pytest.raises(KeyError):
        documents.save(document, lease_owner="same-owner", lease_attempt=first.attempt_count, transition_state=True)
    with pytest.raises(KeyError):
        frontier.complete(identifier, "same-owner", "2026-01-01T00:04:00+00:00", attempt_count=first.attempt_count)
    with pytest.raises(KeyError):
        frontier.release_for_retry(identifier, "same-owner", "2026-01-01T00:04:00+00:00", "late", attempt_count=first.attempt_count)
    assert candidates.get_state(identifier) is UrlState.LIVE
    documents.save(document, lease_owner="same-owner", lease_attempt=second.attempt_count, transition_state=True)
    assert candidates.get_state(identifier) is UrlState.CRAWLED
    assert len(documents.pending_for_index(limit=10)) == 1


def test_failed_document_save_rolls_back_lifecycle(tmp_path):
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    identifier = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(identifier, UrlState.LIVENESS_PENDING)
    candidates.transition(identifier, UrlState.LIVE)
    document = CrawlResultProcessor(candidates).process_html(identifier, "<p>content</p>", transition_state=False)
    with database.connection() as connection:
        connection.execute("CREATE TRIGGER simulate_full BEFORE INSERT ON candidate_state_transitions BEGIN SELECT RAISE(ABORT, 'disk is full'); END")
    with pytest.raises(sqlite3.IntegrityError):
        CrawledDocumentRepository(database).save(document, transition_state=True)
    assert candidates.get_state(identifier) is UrlState.LIVE
    assert CrawledDocumentRepository(database).pending_for_index(limit=10) == ()


def test_idle_scheduler_does_not_rewrite_unrelated_hosts(tmp_path):
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    with database.connection() as connection:
        connection.execute("INSERT INTO hosts(host, updated_at) VALUES ('unrelated.example', 'original')")
    assert FrontierRepository(database).lease_next("worker", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00") is None
    with database.connection() as connection:
        assert connection.execute("SELECT updated_at FROM hosts").fetchone()[0] == "original"


def test_cache_frequency_bookkeeping_is_bounded_under_cold_scans():
    cache = BoundedShardCache(max_entries=2, max_bytes=20)
    for number in range(10_000):
        cache.get_or_load(str(number), 10, lambda: "value")
    assert len(cache._frequency) <= cache.max_frequency_entries
    cache.clear()
    assert cache.snapshot().active_bytes == cache.snapshot().active_entries == 0


def test_serving_shutdown_releases_native_readers(tmp_path):
    pytest.importorskip("tantivy")
    url = canonicalize_url("https://example.com/")
    document = SearchDocument(UrlId.from_canonical_url(url), url.value, "Python", "crawler")
    publish_lexical_generation(tmp_path, "first", {"s": (document,)}, backend="tantivy")
    app, service = create_generation_app(tmp_path, require_signatures=False)
    with TestClient(app) as client:
        assert client.post("/search", json={"query": "python"}).status_code == 200
        index = service.indexes.get("s")
        temporary = index._temporary.name
        del index
    from pathlib import Path
    assert not Path(temporary).exists()
    assert service.stats()["loaded_shards"] == 0
    with pytest.raises(ArtifactValidationError):
        service.search("python")


def test_continuous_crawler_backoff_is_bounded_and_stop_is_cooperative():
    from search_engine.engine.crawler_worker import CrawlAttemptResult

    stop = Event()

    class Worker:
        attempts = 0

        def run_once(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            stop.set()
            return CrawlAttemptResult(True, True)

    worker = Worker()
    result = CrawlerRunner(worker).run_forever("worker", stop=stop, idle_seconds=.001)
    assert worker.attempts == 2 and result.completed == 1

    class Broken:
        def run_once(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk is full")

    with pytest.raises(sqlite3.OperationalError):
        CrawlerRunner(Broken()).run_forever("worker", stop=Event(), idle_seconds=.001, max_consecutive_errors=2)


def test_reliability_drills_include_a_real_child_process_exit(tmp_path):
    import importlib.util
    from pathlib import Path

    pytest.importorskip("psutil")
    path = Path(__file__).resolve().parents[2] / "scripts/reliability_soak.py"
    specification = importlib.util.spec_from_file_location("reliability_soak", path)
    benchmark = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(benchmark)
    report = benchmark.run(tmp_path / "soak", cycles=2, requests_per_cycle=8, backend="reference")
    assert report["passed"]
    assert report["cycles"] == 2 and report["requests"] == 16
    assert "abrupt worker exit" in report["drills"]
