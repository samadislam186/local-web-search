"""Bounded candidate liveness runner that promotes live URLs into crawling."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from urllib.parse import urlsplit

from .liveness import LivenessClassification
from .liveness_worker import LivenessWorker
from .observability import MetricsRegistry, log_event
from .state import CandidateRepository, FrontierRepository

_DEFAULT_LIVE_RECHECK_INTERVAL = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class LivenessRunStats:
    checked: int
    promoted_to_frontier: int
    live: int
    unavailable: int


class LivenessRunner:
    """Checks a finite canonicalized-candidate batch and enqueues live URLs."""

    def __init__(
        self,
        candidates: CandidateRepository,
        worker: LivenessWorker,
        frontier: FrontierRepository,
        *,
        crawl_priority: float = 0.0,
        live_recheck_interval: timedelta = _DEFAULT_LIVE_RECHECK_INTERVAL,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.candidates = candidates
        self.worker = worker
        self.frontier = frontier
        self.crawl_priority = crawl_priority
        if live_recheck_interval <= timedelta():
            raise ValueError("live_recheck_interval must be positive")
        self.live_recheck_interval = live_recheck_interval
        self.metrics = metrics or MetricsRegistry()

    def run(self, *, max_candidates: int, now: datetime | None = None) -> LivenessRunStats:
        checked_at = now or datetime.now(UTC)
        candidate_ids = self.candidates.ids_due_for_liveness(
            checked_at.isoformat(),
            limit=max_candidates,
            live_recheck_before=(checked_at - self.live_recheck_interval).isoformat(),
        )
        promoted = live = unavailable = 0
        for url_id in candidate_ids:
            started = perf_counter()
            classification = self.worker.check(url_id, now=checked_at)
            self.metrics.increment("liveness_checked")
            self.metrics.observe("liveness_latency_ms", (perf_counter() - started) * 1000)
            log_event(logging.getLogger(__name__), "liveness_checked", classification=classification.value)
            if classification is LivenessClassification.LIVE:
                canonical = self.candidates.canonical_url(url_id)
                host = urlsplit(canonical.value).netloc.casefold()
                self.frontier.enqueue(url_id, host, self.crawl_priority)
                promoted += 1
                live += 1
            elif classification is LivenessClassification.TEMPORARILY_UNAVAILABLE:
                unavailable += 1
        return LivenessRunStats(len(candidate_ids), promoted, live, unavailable)
