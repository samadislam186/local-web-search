from datetime import UTC, datetime
from pathlib import Path

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.contracts import (
    CandidateStore,
    DocumentStore,
    FormatVersions,
    FrontierStore,
    OperationError,
    OperationResult,
    utc_timestamp,
)
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    UrlState,
)


def test_repository_contracts_are_runtime_replaceable(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    frontier = FrontierRepository(database)
    documents = CrawledDocumentRepository(database)
    assert isinstance(candidates, CandidateStore)
    assert isinstance(frontier, FrontierStore)
    assert isinstance(documents, DocumentStore)
    url_id = candidates.upsert(canonicalize_url("https://example.com/"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    documents.save(CrawlResultProcessor(candidates).process_html(url_id, "<p>document</p>"))
    assert documents.pending_for_index(limit=1)[0].url_id == url_id


def test_format_versions_reject_non_positive_values() -> None:
    assert FormatVersions(1, 1, 1).artifact == 1
    with pytest.raises(ValueError, match="positive"):
        FormatVersions(1, 0, 1)


def test_operation_result_and_utc_timestamp_contracts() -> None:
    assert OperationResult(value="ok").ok
    assert not OperationResult(error=OperationError("timeout", "timed out", retryable=True)).ok
    with pytest.raises(ValueError, match="exactly"):
        OperationResult()
    with pytest.raises(ValueError, match="timezone"):
        utc_timestamp(datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None))
    assert utc_timestamp(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"
