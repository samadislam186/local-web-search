from datetime import UTC, datetime, timedelta
from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.liveness import DnsResult, DnsStatus
from search_engine.liveness_runner import LivenessRunner
from search_engine.liveness_worker import LivenessWorker
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    LivenessRepository,
    UrlState,
)


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def test_dns_outage_recovers_after_worker_restart_and_persisted_backoff(tmp_path: Path) -> None:
    database_path = tmp_path / "crawl.sqlite3"
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")

    class OutageResolver:
        def resolve(self, _hostname: str, _port: int) -> DnsResult:
            return DnsResult(DnsStatus.TEMPORARY_FAILURE, error="DNS outage")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    worker = LivenessWorker(candidates, LivenessRepository(database), resolver=OutageResolver())
    runner = LivenessRunner(candidates, worker, FrontierRepository(database))
    assert runner.run(max_candidates=1, now=start).unavailable == 1
    assert candidates.get_state(url_id) is UrlState.TEMPORARILY_UNAVAILABLE

    restarted_database = CrawlStateDatabase(database_path)
    restarted_candidates = CandidateRepository(restarted_database)
    restored_worker = LivenessWorker(
        restarted_candidates,
        LivenessRepository(restarted_database),
        resolver=_Resolver(),
        probe=SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(200, {}, b"ok")),
    )
    restarted = LivenessRunner(restarted_candidates, restored_worker, FrontierRepository(restarted_database))
    assert restarted.run(max_candidates=1, now=start + timedelta(minutes=4)).checked == 0
    assert restarted.run(max_candidates=1, now=start + timedelta(minutes=5)).promoted_to_frontier == 1
    assert restarted_candidates.get_state(url_id) is UrlState.LIVE
    assert LivenessRepository(restarted_database).retry_count(url_id) == 0


def test_liveness_runner_promotes_only_live_candidates_to_frontier(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    live_id = candidates.upsert(canonicalize_url("https://live.example/"), "seed")
    candidates.upsert(canonicalize_url("https://dead.example/"), "seed")

    def transport(_scheme: str, hostname: str, *_args: object) -> FetchResult:
        return FetchResult(200 if hostname == "live.example" else 404, {}, b"ok")

    probe = SafeHttpProbe(_Resolver(), transport=transport)
    worker = LivenessWorker(candidates, LivenessRepository(database), resolver=_Resolver(), probe=probe)
    frontier = FrontierRepository(database)
    stats = LivenessRunner(candidates, worker, frontier, crawl_priority=2.0).run(max_candidates=10)
    assert (stats.checked, stats.promoted_to_frontier, stats.live) == (2, 1, 1)
    assert frontier.lease_next("worker", "9999-01-01T00:00:00+00:00", "9999-01-01T00:01:00+00:00") == live_id


def test_liveness_runner_keeps_unavailable_candidates_out_of_frontier(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    probe = SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(503, {}, b""))
    worker = LivenessWorker(candidates, LivenessRepository(database), resolver=_Resolver(), probe=probe)
    stats = LivenessRunner(candidates, worker, FrontierRepository(database)).run(
        max_candidates=1, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert (stats.checked, stats.promoted_to_frontier, stats.unavailable) == (1, 0, 1)


def test_liveness_runner_rechecks_transient_failure_only_after_retry_time(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    responses = iter((FetchResult(503, {}, b""), FetchResult(503, {}, b""), FetchResult(200, {}, b"ok")))
    worker = LivenessWorker(
        candidates,
        LivenessRepository(database),
        resolver=_Resolver(),
        probe=SafeHttpProbe(_Resolver(), transport=lambda *_args: next(responses)),
        retry_delay=timedelta(minutes=5),
    )
    runner = LivenessRunner(candidates, worker, FrontierRepository(database))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert runner.run(max_candidates=1, now=start).unavailable == 1
    assert runner.run(max_candidates=1, now=start + timedelta(minutes=4)).checked == 0
    assert runner.run(max_candidates=1, now=start + timedelta(minutes=5)).unavailable == 1
    assert runner.run(max_candidates=1, now=start + timedelta(minutes=14)).checked == 0
    assert runner.run(max_candidates=1, now=start + timedelta(minutes=15)).promoted_to_frontier == 1


def test_liveness_runner_rechecks_live_candidate_only_after_its_interval(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    responses = iter((FetchResult(200, {}, b"ok"), FetchResult(404, {}, b"")))
    worker = LivenessWorker(
        candidates,
        LivenessRepository(database),
        resolver=_Resolver(),
        probe=SafeHttpProbe(_Resolver(), transport=lambda *_args: next(responses)),
    )
    runner = LivenessRunner(
        candidates,
        worker,
        FrontierRepository(database),
        live_recheck_interval=timedelta(days=1),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert runner.run(max_candidates=1, now=start).live == 1
    assert runner.run(max_candidates=1, now=start + timedelta(hours=23)).checked == 0
    assert runner.run(max_candidates=1, now=start + timedelta(days=1)).checked == 1
