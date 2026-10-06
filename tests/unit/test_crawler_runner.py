from datetime import timedelta
from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawler_runner import CrawlerRunner
from search_engine.crawler_worker import CrawlerWorker, RetryPolicy
from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.liveness import DnsResult, DnsStatus
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    UrlState,
)


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def test_crawler_runner_processes_bounded_work_and_reports_idle(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    candidates.transition(url_id, UrlState.CRAWLABLE)
    frontier = FrontierRepository(database)
    frontier.enqueue(url_id, "example.com", 1.0)
    worker = CrawlerWorker(
        frontier,
        candidates,
        SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(200, {"content-type": "text/html"}, b"<p>the python crawler</p>")),
        CrawlResultProcessor(candidates),
    )
    stats = CrawlerRunner(worker, host_delay=timedelta()).run("worker", max_jobs=2)
    assert (stats.attempted, stats.completed, stats.idle) == (1, 1, True)


def test_retry_policy_is_exponential_and_capped() -> None:
    policy = RetryPolicy(timedelta(seconds=2), timedelta(seconds=5))
    assert policy.delay_for_attempt(1) == timedelta(seconds=2)
    assert policy.delay_for_attempt(2) == timedelta(seconds=4)
    assert policy.delay_for_attempt(3) == timedelta(seconds=5)
