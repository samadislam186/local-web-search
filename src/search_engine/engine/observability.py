"""Thread-safe in-process counters and latency summaries for the MVP."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_SENSITIVE_FIELDS = frozenset({"url", "raw_url", "canonical_url", "authorization", "cookie", "token", "secret"})


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    counters: dict[str, int]
    observations: dict[str, dict[str, float]]


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._observations: dict[str, list[float]] = defaultdict(list)

    def increment(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("counter increment must not be negative")
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, value: float) -> None:
        if value < 0:
            raise ValueError("metric observation must not be negative")
        with self._lock:
            self._observations[name].append(value)

    def snapshot(self) -> MetricSnapshot:
        with self._lock:
            observations = {
                name: {"count": float(len(values)), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}
                for name, values in self._observations.items()
                if values
            }
            return MetricSnapshot(dict(self._counters), observations)


class NoopMetricsRegistry:
    """Metrics-compatible implementation for callers that intentionally disable collection."""

    def increment(self, _name: str, _amount: int = 1) -> None:
        return None

    def observe(self, _name: str, _value: float) -> None:
        return None

    def snapshot(self) -> MetricSnapshot:
        return MetricSnapshot({}, {})


@contextmanager
def correlation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Attach a safe request/job correlation ID to events in this context."""
    value = correlation_id or uuid4().hex
    if not value.strip():
        raise ValueError("correlation_id must not be empty")
    token = _CORRELATION_ID.set(value)
    try:
        yield value
    finally:
        _CORRELATION_ID.reset(token)


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit a structured event while rejecting raw-URL and secret-shaped fields."""
    if not event or not event.replace("_", "").isalnum():
        raise ValueError("event must be a non-empty alphanumeric identifier")
    forbidden = _SENSITIVE_FIELDS & fields.keys()
    if forbidden:
        raise ValueError(f"sensitive event fields are not allowed: {sorted(forbidden)!r}")
    logger.info(event, extra={"event_fields": fields})


class JsonEventFormatter(logging.Formatter):
    """Small JSON formatter for project-owned events only."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "correlation_id": _CORRELATION_ID.get(),
            "fields": fields,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
