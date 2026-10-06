from datetime import timedelta
from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawler_worker import (
    CrawlerWorker,
    RetryPolicy,
    _decode_html_body,
    _next_allowed_at,
)
from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.liveness import DnsResult, DnsStatus
from search_engine.robots_manager import RobotsDecision
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    UrlState,
)


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def _worker(tmp_path: Path, response: FetchResult) -> tuple[CrawlerWorker, CandidateRepository, FrontierRepository, object]:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    candidates.transition(url_id, UrlState.CRAWLABLE)
    frontier = FrontierRepository(database)
    frontier.enqueue(url_id, "example.com", 1.0, "2026-01-01T00:00:00+00:00")
    probe = SafeHttpProbe(_Resolver(), transport=lambda *_args: response)
    return CrawlerWorker(frontier, candidates, probe, CrawlResultProcessor(candidates)), candidates, frontier, url_id


def test_crawler_worker_processes_html_and_completes_frontier_item(tmp_path: Path) -> None:
    worker, candidates, _frontier, url_id = _worker(tmp_path, FetchResult(200, {"content-type": "text/html"}, b"<title>Python</title><p>the python crawler</p>"))
    result = worker.run_once("worker", now="2026-01-01T00:00:00+00:00", lease_expires_at="2026-01-01T00:01:00+00:00", retry_at="2026-01-01T00:02:00+00:00", next_allowed_at="2026-01-01T00:00:01+00:00")
    assert result.completed and result.document is not None
    assert candidates.get_state(url_id) is UrlState.CRAWLED


def test_crawler_worker_hands_redirect_target_to_candidate_discovery(tmp_path: Path) -> None:
    worker, candidates, _frontier, _url_id = _worker(
        tmp_path,
        FetchResult(
            200,
            {"content-type": "text/html"},
            b"<title>Redirected</title>",
            redirect_target="https://target.example/next#fragment",
        ),
    )
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert result.document is not None
    redirected_id = candidates.upsert(canonicalize_url("https://target.example/next"), "check")
    assert redirected_id in result.document.discovered_url_ids
    with candidates.database.connection() as connection:
        provenance = connection.execute(
            "SELECT source, source_record_id FROM candidate_provenance WHERE url_id=? AND source='redirect'",
            (redirected_id.value,),
        ).fetchone()
    assert tuple(provenance) == ("redirect", "https://example.com/")


def test_crawler_worker_requeues_failed_fetch(tmp_path: Path) -> None:
    worker, _candidates, frontier, url_id = _worker(tmp_path, FetchResult(None, {}, b"", error="timeout"))
    result = worker.run_once("worker", now="2026-01-01T00:00:00+00:00", lease_expires_at="2026-01-01T00:01:00+00:00", retry_at="2026-01-01T00:02:00+00:00", next_allowed_at="2026-01-01T00:00:01+00:00")
    assert not result.completed and result.reason == "timeout"
    assert frontier.lease_next("retry", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") == url_id


def test_crawler_worker_honors_retry_after_for_the_entire_host(tmp_path: Path) -> None:
    worker, candidates, frontier, _url_id = _worker(
        tmp_path, FetchResult(429, {"retry-after": "60"}, b"", error="rate_limited")
    )
    second_id = candidates.upsert(canonicalize_url("https://example.com/other"), "seed")
    frontier.enqueue(second_id, "example.com", 1.0, "2026-01-01T00:00:00+00:00")
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert not result.completed
    assert frontier.lease_next("other", "2026-01-01T00:00:59+00:00", "2026-01-01T00:02:00+00:00") is None
    assert frontier.lease_next("other", "2026-01-01T00:01:00+00:00", "2026-01-01T00:02:00+00:00") == second_id


def test_crawler_worker_honors_robots_crawl_delay_for_the_host(tmp_path: Path) -> None:
    worker, candidates, frontier, _url_id = _worker(tmp_path, FetchResult(200, {"content-type": "text/html"}, b"<p>ok</p>"))
    worker.robots_delay = lambda _url: 30.0
    second_id = candidates.upsert(canonicalize_url("https://example.com/other"), "seed")
    frontier.enqueue(second_id, "example.com", 1.0, "2026-01-01T00:00:00+00:00")
    assert worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    ).completed
    assert frontier.lease_next("other", "2026-01-01T00:00:29+00:00", "2026-01-01T00:02:00+00:00") is None
    assert frontier.lease_next("other", "2026-01-01T00:00:30+00:00", "2026-01-01T00:02:00+00:00") == second_id


def test_retry_after_http_date_uses_the_later_host_delay() -> None:
    now = "2026-01-01T00:00:00+00:00"
    assert _next_allowed_at("2026-01-01T00:00:01+00:00", "Thu, 01 Jan 2026 00:02:00 GMT", now) == (
        "2026-01-01T00:02:00+00:00"
    )
    assert _next_allowed_at("2026-01-01T00:00:01+00:00", "invalid", now) == "2026-01-01T00:00:01+00:00"
    assert _next_allowed_at(
        "2026-01-01T00:00:01+00:00", "999999", now, maximum_delay=timedelta(minutes=10)
    ) == "2026-01-01T00:10:00+00:00"


def test_html_decoder_honors_declared_charset_and_falls_back_safely() -> None:
    assert _decode_html_body("caf\xe9".encode("latin-1"), "text/html; charset=iso-8859-1") == "café"
    assert _decode_html_body(b"text", "text/html; charset=not-a-codec") == "text"


def test_crawler_worker_marks_successful_non_html_as_unusable(tmp_path: Path) -> None:
    worker, candidates, _frontier, url_id = _worker(
        tmp_path, FetchResult(200, {"content-type": "application/pdf"}, b"%PDF")
    )
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert result.completed and result.reason == "unsupported_content_type:application/pdf"
    assert candidates.get_state(url_id) is UrlState.LIVE_BUT_UNUSABLE


def test_crawler_worker_requeues_unexpected_processing_failure(tmp_path: Path) -> None:
    worker, _candidates, frontier, url_id = _worker(
        tmp_path, FetchResult(200, {"content-type": "text/html"}, b"<p>content</p>")
    )

    class FailingProcessor:
        def process_html(self, _url_id: object, _html: str) -> object:
            raise ValueError("parser failure")

    worker.processor = FailingProcessor()  # type: ignore[assignment]
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert not result.completed and result.reason == "processing:ValueError"
    assert frontier.lease_next("retry", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") == url_id


def test_crawler_worker_requeues_unexpected_probe_failure(tmp_path: Path) -> None:
    worker, _candidates, frontier, url_id = _worker(
        tmp_path, FetchResult(200, {"content-type": "text/html"}, b"<p>content</p>")
    )
    worker.probe.probe = lambda _url: (_ for _ in ()).throw(RuntimeError("transport failure"))  # type: ignore[method-assign]
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert not result.completed and result.reason == "probe:RuntimeError"
    assert frontier.lease_next("retry", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") == url_id


def test_crawler_worker_requeues_when_robots_policy_is_unavailable(tmp_path: Path) -> None:
    worker, _candidates, frontier, url_id = _worker(
        tmp_path, FetchResult(200, {"content-type": "text/html"}, b"<p>content</p>")
    )
    worker.robots_check = lambda _url: RobotsDecision.UNAVAILABLE
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert not result.completed and result.reason == "robots:unavailable"
    assert frontier.lease_next("retry", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") == url_id


def test_crawler_worker_stops_retrying_after_configured_attempt_limit(tmp_path: Path) -> None:
    worker, candidates, frontier, url_id = _worker(tmp_path, FetchResult(None, {}, b"", error="timeout"))
    worker.retry_policy = RetryPolicy(max_attempts=1)
    result = worker.run_once(
        "worker",
        now="2026-01-01T00:00:00+00:00",
        lease_expires_at="2026-01-01T00:01:00+00:00",
        retry_at="2026-01-01T00:02:00+00:00",
        next_allowed_at="2026-01-01T00:00:01+00:00",
    )
    assert result.completed and result.reason == "retry_exhausted:timeout"
    assert candidates.get_state(url_id) is UrlState.TEMPORARILY_UNAVAILABLE
    assert frontier.lease_next("retry", "2026-01-01T00:02:00+00:00", "2026-01-01T00:03:00+00:00") is None
