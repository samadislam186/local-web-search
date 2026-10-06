from pathlib import Path

from search_engine.canonicalization import canonicalize_url
from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.liveness import DnsResult, DnsStatus, LivenessClassification
from search_engine.liveness_worker import LivenessWorker
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    LivenessRepository,
    UrlState,
)


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def test_liveness_worker_records_evidence_and_transitions_live_url(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    probe = SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(200, {}, b"ok", tls_status="valid"))
    result = LivenessWorker(candidates, LivenessRepository(database), resolver=_Resolver(), probe=probe).check(url_id)
    assert result is LivenessClassification.LIVE
    assert candidates.get_state(url_id) is UrlState.LIVE


def test_liveness_recheck_updates_previously_live_candidate(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    probe = SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(404, {}, b""))
    result = LivenessWorker(candidates, LivenessRepository(database), resolver=_Resolver(), probe=probe).check(url_id)
    assert result is LivenessClassification.DEAD
    assert candidates.get_state(url_id) is UrlState.DEAD


def test_liveness_recheck_replaces_live_state_with_unknown_and_schedules_retry(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    probe = SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(None, {}, b"", error="timeout"))
    result = LivenessWorker(candidates, LivenessRepository(database), resolver=_Resolver(), probe=probe).check(url_id)
    assert result is LivenessClassification.UNKNOWN
    assert candidates.get_state(url_id) is UrlState.UNKNOWN
    assert LivenessRepository(database).retry_count(url_id) == 1
