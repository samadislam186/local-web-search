"""Local-only, aggregate-first feedback primitives for later ranking improvements."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blake3 import blake3


class FeedbackKind(StrEnum):
    IMPRESSION = "impression"
    SELECT = "select"
    UPVOTE = "upvote"
    DOWNVOTE = "downvote"
    CITED = "cited"


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    """No raw query text: only a locally salted coarse query-class identifier."""

    query_class: str
    url_id: str
    kind: FeedbackKind


def classify_query(query: str, *, local_salt: bytes) -> str:
    """Create a local, non-portable coarse key for aggregate feedback buckets."""
    normalized = " ".join(query.casefold().split())
    if not normalized:
        raise ValueError("query must not be empty")
    bucket = " ".join(normalized.split()[:4])
    return blake3(local_salt + bucket.encode("utf-8")).hexdigest(length=16)


class LocalFeedbackAggregator:
    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str, FeedbackKind]] = Counter()

    def record(self, event: FeedbackEvent) -> None:
        if len(event.url_id) != 32 or event.url_id != event.url_id.lower():
            raise ValueError("feedback must refer to a 128-bit UrlId hex value")
        try:
            if len(bytes.fromhex(event.url_id)) != 16:
                raise ValueError
        except ValueError as exc:
            raise ValueError("feedback must refer to a 128-bit UrlId hex value") from exc
        self._counts[(event.query_class, event.url_id, event.kind)] += 1

    def ranking_delta(self, query_class: str, url_id: str) -> float:
        """Conservative local adjustment; ranking remains baseline-dominated."""
        selected = self._counts[(query_class, url_id, FeedbackKind.SELECT)]
        upvotes = self._counts[(query_class, url_id, FeedbackKind.UPVOTE)]
        citations = self._counts[(query_class, url_id, FeedbackKind.CITED)]
        downvotes = self._counts[(query_class, url_id, FeedbackKind.DOWNVOTE)]
        impressions = self._counts[(query_class, url_id, FeedbackKind.IMPRESSION)]
        if impressions == 0:
            return 0.0
        return max(-0.1, min(0.1, (selected + upvotes + citations - downvotes) / impressions * 0.1))

    def contribution(self, *, minimum_events: int = 10) -> dict[str, dict[str, int]]:
        """Emit aggregate buckets only; do not emit raw local event history."""
        if minimum_events <= 0:
            raise ValueError("minimum_events must be positive")
        grouped: dict[tuple[str, str], dict[str, int]] = {}
        for (query_class, url_id, kind), count in self._counts.items():
            grouped.setdefault((query_class, url_id), {})[kind.value] = count
        return {
            f"{query_class}:{url_id}": counts
            for (query_class, url_id), counts in grouped.items()
            if sum(counts.values()) >= minimum_events
        }


class SqliteFeedbackAggregator:
    """Restart-safe aggregate feedback store; raw queries are never persisted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS feedback_counts (
                    query_class TEXT NOT NULL,
                    url_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK(count >= 0),
                    PRIMARY KEY(query_class, url_id, kind)
                )"""
            )

    def record(self, event: FeedbackEvent) -> None:
        _validate_event(event)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO feedback_counts(query_class, url_id, kind, count) VALUES (?, ?, ?, 1)
                   ON CONFLICT(query_class, url_id, kind) DO UPDATE SET count=count + 1""",
                (event.query_class, event.url_id, event.kind.value),
            )

    def ranking_delta(self, query_class: str, url_id: str) -> float:
        counts = self._counts(query_class, url_id)
        return _ranking_delta(counts)

    def contribution(self, *, minimum_events: int = 10) -> dict[str, dict[str, int]]:
        if minimum_events <= 0:
            raise ValueError("minimum_events must be positive")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT query_class, url_id, kind, count FROM feedback_counts ORDER BY query_class, url_id, kind"
            ).fetchall()
        grouped: dict[tuple[str, str], dict[str, int]] = {}
        for query_class, url_id, kind, count in rows:
            grouped.setdefault((str(query_class), str(url_id)), {})[str(kind)] = int(count)
        return {
            f"{query_class}:{url_id}": counts
            for (query_class, url_id), counts in grouped.items()
            if sum(counts.values()) >= minimum_events
        }

    def _counts(self, query_class: str, url_id: str) -> Counter[FeedbackKind]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT kind, count FROM feedback_counts WHERE query_class=? AND url_id=?", (query_class, url_id)
            ).fetchall()
        return Counter({FeedbackKind(str(kind)): int(count) for kind, count in rows})

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)


def _validate_event(event: FeedbackEvent) -> None:
    if len(event.url_id) != 32 or event.url_id != event.url_id.lower():
        raise ValueError("feedback must refer to a 128-bit UrlId hex value")
    try:
        if len(bytes.fromhex(event.url_id)) != 16:
            raise ValueError
    except ValueError as exc:
        raise ValueError("feedback must refer to a 128-bit UrlId hex value") from exc


def _ranking_delta(counts: Counter[FeedbackKind]) -> float:
    selected = counts[FeedbackKind.SELECT]
    upvotes = counts[FeedbackKind.UPVOTE]
    citations = counts[FeedbackKind.CITED]
    downvotes = counts[FeedbackKind.DOWNVOTE]
    impressions = counts[FeedbackKind.IMPRESSION]
    if impressions == 0:
        return 0.0
    return max(-0.1, min(0.1, (selected + upvotes + citations - downvotes) / impressions * 0.1))
