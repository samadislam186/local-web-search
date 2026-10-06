"""Bounded crawl-worker runner for local/process-managed execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .crawler_worker import CrawlerWorker


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
    ) -> None:
        if lease_duration <= timedelta() or host_delay < timedelta():
            raise ValueError("invalid crawler timing configuration")
        self.worker = worker
        self.lease_duration = lease_duration
        self.host_delay = host_delay

    def run(self, owner: str, *, max_jobs: int) -> CrawlRunStats:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        attempted = completed = requeued = 0
        for _ in range(max_jobs):
            now = datetime.now(UTC)
            result = self.worker.run_once(
                owner,
                now=now.isoformat(),
                lease_expires_at=(now + self.lease_duration).isoformat(),
                retry_at=None,
                next_allowed_at=(now + self.host_delay).isoformat(),
            )
            if not result.leased:
                return CrawlRunStats(attempted, completed, requeued, True)
            attempted += 1
            completed += int(result.completed)
            requeued += int(not result.completed)
        return CrawlRunStats(attempted, completed, requeued, False)
