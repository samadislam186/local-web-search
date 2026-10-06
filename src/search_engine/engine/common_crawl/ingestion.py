"""Common Crawl CDX-index JSONL ingestion without corpus-sized allocations."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from time import monotonic

from ..canonicalization import canonicalize_url
from ..errors import UrlCanonicalizationError
from ..state import CandidateRepository, IngestionProgressRepository

SOURCE = "common_crawl"


@dataclass(frozen=True, slots=True)
class IngestionStats:
    lines_seen: int = 0
    candidates_written: int = 0
    duplicate_or_existing: int = 0
    rejected: int = 0
    duration_seconds: float = 0.0
    lines_per_second: float = 0.0


class CommonCrawlIngestor:
    """Ingest one CDX partition at a time, checkpointing after every accepted record.

    Callers provide an iterable of JSONL strings from a downloaded/streamed index
    response. This keeps network transport separate from deterministic ingestion.
    """

    def __init__(
        self,
        candidates: CandidateRepository,
        progress: IngestionProgressRepository,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.candidates = candidates
        self.progress = progress
        self.clock = clock

    def ingest_partition(
        self, lines: Iterable[str], *, crawl_id: str, partition: str
    ) -> IngestionStats:
        checkpoint = self.progress.last_committed_line(SOURCE, crawl_id, partition)
        started_at = self.clock()
        stats = IngestionStats()
        for line_number, line in enumerate(lines, start=1):
            if line_number <= checkpoint:
                continue
            stats = IngestionStats(
                lines_seen=stats.lines_seen + 1,
                candidates_written=stats.candidates_written,
                duplicate_or_existing=stats.duplicate_or_existing,
                rejected=stats.rejected,
            )
            try:
                record = _parse_record(line)
                canonical = canonicalize_url(record["url"])
                _, created = self.candidates.upsert_with_result(
                    canonical,
                    SOURCE,
                    source_record_id=_record_id(record, crawl_id),
                    observed_at=str(record.get("timestamp") or "") or None,
                    metadata_json=_provenance_json(record, crawl_id),
                )
            except (json.JSONDecodeError, KeyError, TypeError, UrlCanonicalizationError):
                stats = IngestionStats(
                    lines_seen=stats.lines_seen,
                    candidates_written=stats.candidates_written,
                    duplicate_or_existing=stats.duplicate_or_existing,
                    rejected=stats.rejected + 1,
                )
            else:
                stats = IngestionStats(
                    lines_seen=stats.lines_seen,
                    candidates_written=stats.candidates_written + int(created),
                    duplicate_or_existing=stats.duplicate_or_existing + int(not created),
                    rejected=stats.rejected,
                )
            self.progress.commit(SOURCE, crawl_id, partition, line_number)
        self.progress.commit(
            SOURCE,
            crawl_id,
            partition,
            self.progress.last_committed_line(SOURCE, crawl_id, partition),
            "complete",
        )
        duration_seconds = max(0.0, self.clock() - started_at)
        return replace(
            stats,
            duration_seconds=duration_seconds,
            lines_per_second=stats.lines_seen / duration_seconds if duration_seconds else 0.0,
        )


def _parse_record(line: str) -> dict[str, object]:
    record = json.loads(line)
    if not isinstance(record, dict) or not isinstance(record.get("url"), str):
        raise TypeError("Common Crawl index record must contain a string url")
    return record


def _record_id(record: dict[str, object], crawl_id: str) -> str:
    """Stable provenance identity where the CDX response gives location details."""
    return ":".join(
        str(value)
        for value in (crawl_id, *(record.get(key, "") for key in ("filename", "offset", "length", "timestamp", "url")))
    )


def _provenance_json(record: dict[str, object], crawl_id: str) -> str:
    retained = {
        key: record[key]
        for key in ("crawl", "timestamp", "mime", "status", "digest", "filename", "offset", "length")
        if key in record
    }
    retained.setdefault("crawl", crawl_id)
    return json.dumps(retained, sort_keys=True, separators=(",", ":"))
