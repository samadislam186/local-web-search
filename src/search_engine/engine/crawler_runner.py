"""Bounded crawl-worker runner for local/process-managed execution."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from time import perf_counter

from .crawler_worker import CrawlerWorker
from .observability import MetricsRegistry, log_event


@dataclass(frozen=True, slots=True)
class CrawlRunStats:
    attempted: int
    completed: int
    requeued: int
    idle: bool


class CrawlerRunner:
    def __init__(
        self,
        worker: CrawlerWorker,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
        host_delay: timedelta = timedelta(seconds=1),
        metrics: MetricsRegistry | None = None,
    ) -> None:
        if lease_duration <= timedelta() or host_delay < timedelta():
            raise ValueError("invalid crawler timing configuration")
        self.worker = worker
        self.lease_duration = lease_duration
        self.host_delay = host_delay
        self.metrics = metrics or MetricsRegistry()

    def run(self, owner: str, *, max_jobs: int) -> CrawlRunStats:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        attempted = completed = requeued = 0
        for _ in range(max_jobs):
            now = datetime.now(UTC)
            started = perf_counter()
            result = self.worker.run_once(
                owner,
                now=now.isoformat(),
                lease_expires_at=(now + self.lease_duration).isoformat(),
                retry_at=None,
                next_allowed_at=(now + self.host_delay).isoformat(),
            )
            self.metrics.observe("crawl_attempt_latency_ms", (perf_counter() - started) * 1000)
            if not result.leased:
                return CrawlRunStats(attempted, completed, requeued, True)
            attempted += 1
            completed += int(result.completed)
            requeued += int(not result.completed)
            self.metrics.increment("crawl_attempts")
            self.metrics.increment("crawl_completed", int(result.completed))
            self.metrics.increment("crawl_requeued", int(not result.completed))
            log_event(logging.getLogger(__name__), "crawl_attempt_completed", completed=result.completed)
        return CrawlRunStats(attempted, completed, requeued, False)

    def run_forever(self, owner: str, *, stop: Event, batch_size: int = 100,
                    idle_seconds: float = 1.0, max_consecutive_errors: int = 5) -> CrawlRunStats:
        """Process bounded batches until stopped; database faults get bounded backoff.

        Programming/schema errors are not suppressed. An interrupted attempt is
        recoverable through its persisted lease, rather than silently discarded.
        """
        if batch_size <= 0 or idle_seconds <= 0 or max_consecutive_errors <= 0:
            raise ValueError("invalid continuous crawler configuration")
        attempted = completed = requeued = consecutive_errors = 0
        while not stop.is_set():
            try:
                # Check cancellation between jobs, not only between large batches.
                for _ in range(batch_size):
                    if stop.is_set():
                        break
                    result = self.run(owner, max_jobs=1)
                    attempted += result.attempted
                    completed += result.completed
                    requeued += result.requeued
                    consecutive_errors = 0
                    if result.idle:
                        stop.wait(idle_seconds)
                        break
            except sqlite3.OperationalError as exc:
                if not any(value in str(exc).lower() for value in ("locked", "busy", "disk is full", "disk i/o error")):
                    raise
                consecutive_errors += 1
                self.metrics.increment("crawl_runtime_errors")
                log_event(logging.getLogger(__name__), "crawl_runtime_retry", error=type(exc).__name__)
                if consecutive_errors >= max_consecutive_errors:
                    raise
                stop.wait(min(idle_seconds * (2 ** (consecutive_errors - 1)), 30.0))
        return CrawlRunStats(attempted, completed, requeued, True)
