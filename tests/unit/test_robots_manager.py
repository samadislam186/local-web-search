from datetime import UTC, datetime
from pathlib import Path

from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.liveness import DnsResult, DnsStatus
from search_engine.robots_manager import RobotsDecision, RobotsManager
from search_engine.state import CrawlStateDatabase, HostRepository


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def test_robots_manager_fetches_caches_and_enforces_policy(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return FetchResult(200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private\n")

    manager = RobotsManager(HostRepository(database), SafeHttpProbe(_Resolver(), transport=transport))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert manager.allows("https://example.com/public", "OpenSearchLocalBot", now)
    assert not manager.allows("https://example.com/private/a", "OpenSearchLocalBot", now)
    assert manager.crawl_delay("https://example.com/private/a", "OpenSearchLocalBot", now) is None
    assert calls == 1


def test_robots_manager_blocks_when_policy_is_unavailable(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    manager = RobotsManager(HostRepository(database), SafeHttpProbe(_Resolver(), transport=lambda *_args: FetchResult(None, {}, b"", error="timeout")))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert manager.check("https://example.com/", "OpenSearchLocalBot", now) is RobotsDecision.UNAVAILABLE
    assert not manager.allows("https://example.com/", "OpenSearchLocalBot", now)
