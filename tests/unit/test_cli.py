from pathlib import Path

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.fetch import FetchResult, SafeHttpProbe
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.liveness import DnsResult, DnsStatus
from search_engine.publisher import publish_lexical_generation
from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState

from search_engine.cli import (
    _build_index,
    _ingest_common_crawl,
    _list_common_crawl,
    _parse_pinned_keys,
    _run_liveness,
    _seed_urls,
    _serve,
)


def test_serve_loads_local_generation_and_starts_uvicorn(tmp_path: Path, monkeypatch) -> None:
    canonical = canonicalize_url("https://example.com/")
    document = SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python", "python crawler")
    publish_lexical_generation(tmp_path, "g1", {"s1": (document,)})
    captured: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update({"app": app, **kwargs}))
    _serve(tmp_path, "127.0.0.1", 8080, True)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8080


def test_serve_requires_pinned_key_for_signed_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pinned-key"):
        _serve(tmp_path, "127.0.0.1", 8080, False)
    assert _parse_pinned_keys(["key-1=base64value"]) == {"key-1": "base64value"}
    with pytest.raises(ValueError):
        _parse_pinned_keys(["missing-separator"])


def test_serve_requires_an_explicit_valid_salt_for_local_feedback(tmp_path: Path, monkeypatch) -> None:
    canonical = canonicalize_url("https://example.com/")
    document = SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python", "python crawler")
    publish_lexical_generation(tmp_path, "g1", {"s1": (document,)})
    with pytest.raises(ValueError, match="feedback-salt"):
        _serve(tmp_path, "127.0.0.1", 8080, True, enable_local_feedback=True)
    with pytest.raises(ValueError, match="at least 16"):
        _serve(
            tmp_path,
            "127.0.0.1",
            8080,
            True,
            enable_local_feedback=True,
            feedback_salt_hex="00" * 15,
        )
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    feedback_database = tmp_path / "local-feedback.sqlite3"
    _serve(
        tmp_path,
        "127.0.0.1",
        8080,
        True,
        enable_local_feedback=True,
        feedback_salt_hex="00" * 16,
        feedback_database=feedback_database,
    )
    assert feedback_database.exists()


def test_build_index_cli_helper_publishes_pending_crawl_documents(tmp_path: Path) -> None:
    database_path = tmp_path / "crawl.sqlite3"
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/python"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    crawled = CrawlResultProcessor(candidates).process_html(url_id, "<title>Python</title><p>crawler</p>")
    CrawledDocumentRepository(database).save(crawled)
    result = _build_index(database_path, tmp_path / "artifacts", "g000001", 10, 1)
    assert (result.manifest.generation, result.document_count) == ("g000001", 1)


def test_liveness_cli_helper_promotes_live_candidate(tmp_path: Path) -> None:
    class Resolver:
        def resolve(self, _hostname: str, _port: int) -> DnsResult:
            return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))

    database_path = tmp_path / "crawl.sqlite3"
    database = CrawlStateDatabase(database_path)
    database.initialize()
    CandidateRepository(database).upsert(canonicalize_url("https://example.com/"), "seed")
    stats = _run_liveness(
        database_path,
        10,
        1.0,
        probe=SafeHttpProbe(Resolver(), transport=lambda *_args: FetchResult(200, {}, b"ok")),
    )
    assert (stats.checked, stats.promoted_to_frontier) == (1, 1)
    with pytest.raises(ValueError, match="live_recheck_hours"):
        _run_liveness(database_path, 10, 1.0, 0.0)


def test_seed_cli_helper_canonicalizes_and_deduplicates_urls(tmp_path: Path) -> None:
    database_path = tmp_path / "crawl.sqlite3"
    result = _seed_urls(database_path, ("HTTPS://Example.com", "https://example.com/"), "manual")
    assert (result.submitted, result.created, result.existing) == (2, 1, 1)


def test_common_crawl_cli_helper_streams_into_resumable_ingestion(tmp_path: Path) -> None:
    class Client:
        def stream_index(self, _crawl_id: str, _pattern: str, *, match_type: str):
            assert match_type == "domain"
            return iter(('{"url":"https://example.com/","offset":"1"}',))

    stats = _ingest_common_crawl(
        tmp_path / "crawl.sqlite3", "CC-MAIN-2026-01", "example.com/*", "test", "domain", client=Client()
    )
    assert (stats.lines_seen, stats.candidates_written, stats.rejected) == (1, 1, 0)
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    from search_engine.state import CommonCrawlPlanRepository

    plan = CommonCrawlPlanRepository(database).get("CC-MAIN-2026-01", "test")
    assert plan is not None and plan.url_pattern == "example.com/*"


def test_common_crawl_list_cli_helper_limits_results() -> None:
    class Client:
        def list_collections(self):
            from search_engine.engine.common_crawl.client import CommonCrawlCollection

            return (
                CommonCrawlCollection("first", "First", "https://index.example/first"),
                CommonCrawlCollection("second", "Second", "https://index.example/second"),
            )

    assert [item.crawl_id for item in _list_common_crawl(1, client=Client())] == ["first"]
