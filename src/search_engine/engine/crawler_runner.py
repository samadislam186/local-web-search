"""Bounded crawl-worker runner for local/process-managed execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
