import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.errors import DatabaseSchemaError, InvalidLifecycleTransition, UrlCollisionError
from search_engine.identity import UrlId
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    LivenessRepository,
    UrlState,
)


def test_candidate_is_idempotent_and_wal_enabled(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    repository = CandidateRepository(database)
    url_id = repository.upsert(canonicalize_url("https://example.com"), "operator_seed")
    assert repository.upsert(canonicalize_url("https://example.com/"), "operator_seed") == url_id
    assert repository.get_state(url_id) is UrlState.CANONICALIZED
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CrawlStateDatabase.SCHEMA_VERSION


def test_newer_crawl_state_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "crawl.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(DatabaseSchemaError, match="newer"):
        CrawlStateDatabase(path).initialize()


def test_existing_state_database_is_migrated_to_add_liveness_retry_count(tmp_path: Path) -> None:
    path = tmp_path / "crawl.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE liveness (
                url_id BLOB PRIMARY KEY, dns_last_checked TEXT, dns_status TEXT,
                http_last_checked TEXT, http_status INTEGER, tls_status TEXT,
                redirect_target TEXT, failure_reason TEXT, retry_after TEXT
            )"""
        )
        connection.execute("PRAGMA user_version=1")
    database = CrawlStateDatabase(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(liveness)")}
        assert "retry_count" in columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_forced_url_id_collision_is_quarantined_with_diagnostics(tmp_path: Path, monkeypatch) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    first = candidates.upsert(canonicalize_url("https://first.example/"), "seed")
    monkeypatch.setattr(UrlId, "from_canonical_url", classmethod(lambda _cls, _url: first))
    with pytest.raises(UrlCollisionError):
        candidates.upsert(canonicalize_url("https://second.example/"), "seed")
    collision = candidates.collision_records()[0]
    assert collision.url_id == first
    assert collision.stored_canonical_url == "https://first.example/"
    assert collision.incoming_canonical_url == "https://second.example/"


def test_state_inspection_and_consistent_backup(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    CandidateRepository(database).upsert(canonicalize_url("https://example.com/"), "seed")
    inspection = database.inspect()
    assert inspection.candidates_by_state == {"canonicalized": 1}
    backup = database.backup_to(tmp_path / "backups/crawl.sqlite3")
    restored = CrawlStateDatabase(backup)
    assert restored.inspect().candidates_by_state == {"canonicalized": 1}


def test_competing_workers_cannot_lease_the_same_frontier_item(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidate = CandidateRepository(database).upsert(canonicalize_url("https://example.com/"), "seed")
    frontier = FrontierRepository(database)
    frontier.enqueue(candidate, "example.com", 1.0, "2026-01-01T00:00:00+00:00")

    def lease(owner: str):
        return frontier.lease_next(owner, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00")

    with ThreadPoolExecutor(max_workers=2) as workers:
        leases = tuple(workers.map(lease, ("worker-a", "worker-b")))
    assert leases.count(candidate) == 1
    assert leases.count(None) == 1


def test_lifecycle_rejects_skipped_transitions(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    repository = CandidateRepository(database)
    url_id = repository.upsert(canonicalize_url("https://example.com"), "operator_seed")
    with pytest.raises(InvalidLifecycleTransition):
        repository.transition(url_id, UrlState.INDEXED)
    repository.transition(url_id, UrlState.LIVENESS_PENDING)
    repository.transition(url_id, UrlState.LIVE)
    repository.transition(url_id, UrlState.CRAWLABLE)
    assert repository.get_state(url_id) is UrlState.CRAWLABLE
    history = repository.transition_history(url_id)
    assert [(item.from_state, item.to_state) for item in history] == [
        (None, UrlState.CANONICALIZED),
        (UrlState.CANONICALIZED, UrlState.LIVENESS_PENDING),
        (UrlState.LIVENESS_PENDING, UrlState.LIVE),
        (UrlState.LIVE, UrlState.CRAWLABLE),
    ]
    assert history[0].reason == "candidate_created"


def test_repository_connections_enforce_foreign_keys_and_provenance_is_idempotent(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url = canonicalize_url("https://example.com/")
    candidates.upsert(url, "operator_seed")
    candidates.upsert(url, "operator_seed")
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidate_provenance").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        LivenessRepository(database).record_dns(UrlId(b"x" * 16), "resolvable")


def test_expired_host_lease_is_reclaimed(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    first = candidates.upsert(canonicalize_url("https://example.com/a"), "seed")
    second = candidates.upsert(canonicalize_url("https://example.com/b"), "seed")
    frontier = FrontierRepository(database)
    frontier.enqueue(first, "example.com", 2.0, "2026-01-01T00:00:00+00:00")
    frontier.enqueue(second, "example.com", 1.0, "2026-01-01T00:00:00+00:00")
    assert frontier.lease_next("dead-worker", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00") == first
    assert frontier.lease_next("new-worker", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") == first


def test_frontier_completion_persists_failure_history(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidate = CandidateRepository(database).upsert(canonicalize_url("https://example.com/"), "seed")
    frontier = FrontierRepository(database)
    frontier.enqueue(candidate, "example.com", 1.0, "2026-01-01T00:00:00+00:00")
    assert frontier.lease_next("worker", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00") == candidate
    frontier.complete(candidate, "worker", "2026-01-01T00:00:01+00:00", error="timeout")
    with database.connection() as connection:
        assert connection.execute("SELECT error FROM crawl_attempts").fetchone()[0] == "timeout"
