import json
from pathlib import Path

import pytest
from search_engine.common_crawl import CommonCrawlIngestor
from search_engine.state import CandidateRepository, CrawlStateDatabase, IngestionProgressRepository


def test_common_crawl_ingestion_is_resumable_and_rejects_bad_records(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    progress = IngestionProgressRepository(database)
    ingestor = CommonCrawlIngestor(CandidateRepository(database), progress)
    lines = [
        '{"url":"HTTP://example.com/a#fragment","timestamp":"20260101000000","status":"200","digest":"abc"}',
        '{"url":"ftp://invalid.example"}',
        'not-json',
    ]
    stats = ingestor.ingest_partition(lines, crawl_id="CC-MAIN-2026-01", partition="0")
    assert (stats.lines_seen, stats.candidates_written, stats.rejected) == (3, 1, 2)
    assert progress.last_committed_line("common_crawl", "CC-MAIN-2026-01", "0") == 3
    assert ingestor.ingest_partition(lines, crawl_id="CC-MAIN-2026-01", partition="0").lines_seen == 0


def test_duplicate_records_are_counted_separately_from_new_candidates(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    ingestor = CommonCrawlIngestor(CandidateRepository(database), IngestionProgressRepository(database))
    stats = ingestor.ingest_partition(
        ['{"url":"https://example.com/","offset":"1"}', '{"url":"https://example.com/","offset":"2"}'],
        crawl_id="CC-MAIN-2026-01",
        partition="duplicates",
    )
    assert (stats.candidates_written, stats.duplicate_or_existing, stats.rejected) == (1, 1, 0)


def test_ingestion_preserves_crawl_provenance_when_cdx_omits_it(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    ingestor = CommonCrawlIngestor(CandidateRepository(database), IngestionProgressRepository(database))
    ingestor.ingest_partition(
        ['{"url":"https://example.com/","timestamp":"20260101000000","mime":"text/html","offset":"1"}'],
        crawl_id="CC-MAIN-2026-01",
        partition="provenance",
    )
    with database.connection() as connection:
        row = connection.execute("SELECT source_record_id, observed_at, metadata_json FROM candidate_provenance").fetchone()
    assert row is not None
    assert row[0].startswith("CC-MAIN-2026-01:")
    assert row[1] == "20260101000000"
    assert json.loads(row[2]) == {
        "crawl": "CC-MAIN-2026-01",
        "mime": "text/html",
        "offset": "1",
        "timestamp": "20260101000000",
    }


def test_ingestion_reports_deterministic_throughput(tmp_path: Path) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    clock_values = iter((10.0, 12.0))
    ingestor = CommonCrawlIngestor(
        CandidateRepository(database),
        IngestionProgressRepository(database),
        clock=lambda: next(clock_values),
    )
    stats = ingestor.ingest_partition(
        ['{"url":"https://one.example/"}', '{"url":"https://two.example/"}'],
        crawl_id="CC-MAIN-2026-01",
        partition="metrics",
    )
    assert stats.duration_seconds == 2.0
    assert stats.lines_per_second == 1.0


def test_unexpected_write_failure_does_not_advance_checkpoint(tmp_path: Path, monkeypatch) -> None:
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    progress = IngestionProgressRepository(database)
    monkeypatch.setattr(candidates, "upsert_with_result", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        CommonCrawlIngestor(candidates, progress).ingest_partition(
            ['{"url":"https://example.com/"}'], crawl_id="CC-MAIN-2026-01", partition="failure"
        )
    assert progress.last_committed_line("common_crawl", "CC-MAIN-2026-01", "failure") == 0
