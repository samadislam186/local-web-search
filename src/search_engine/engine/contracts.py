"""Stable cross-subsystem contracts and version metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .canonicalization import CanonicalUrl
from .crawl_processing import CrawledDocument
from .identity import UrlId
from .index import SearchDocument, SearchIndex

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FormatVersions:
    canonicalization: int
    url_id: int
    artifact: int

    def __post_init__(self) -> None:
        if min(self.canonicalization, self.url_id, self.artifact) <= 0:
            raise ValueError("all format versions must be positive")


@dataclass(frozen=True, slots=True)
class OperationError:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("operation errors require code and message")


@dataclass(frozen=True, slots=True)
class OperationResult(Generic[T]):
    value: T | None = None
    error: OperationError | None = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.error is None):
            raise ValueError("an operation result must contain exactly one of value or error")

    @property
    def ok(self) -> bool:
        return self.error is None


def utc_timestamp(value: datetime | None = None) -> str:
    """Return a timezone-aware UTC timestamp in stable ISO-8601 form."""
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat()


@runtime_checkable
class CandidateStore(Protocol):
    def upsert(self, canonical_url: CanonicalUrl, source: str, **kwargs: object) -> UrlId: ...

    def canonical_url(self, url_id: UrlId) -> CanonicalUrl: ...


@runtime_checkable
class FrontierStore(Protocol):
    def enqueue(self, url_id: UrlId, host: str, priority: float, available_at: str | None = None) -> None: ...

    def lease_next(self, owner: str, now: str, lease_expires_at: str) -> UrlId | None: ...


@runtime_checkable
class DocumentStore(Protocol):
    def save(self, document: CrawledDocument) -> None: ...

    def pending_for_index(self, *, limit: int) -> tuple[SearchDocument, ...]: ...

    def mark_indexed(self, url_ids: Iterable[UrlId], generation: str) -> None: ...


@runtime_checkable
class IndexFactory(Protocol):
    def open(self, generation: str, shard_id: str) -> SearchIndex: ...
