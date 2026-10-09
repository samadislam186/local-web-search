"""Read-only serving orchestration: plan, bounded local retrieval, merge, rank."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from urllib.parse import urlsplit

from .feedback import FeedbackEvent, FeedbackKind, LocalFeedbackAggregator, classify_query
from .index import SearchHit, SearchIndex
from .index_provider import IndexProvider, InMemoryIndexProvider
from .observability import MetricsRegistry, log_event
from .query_analysis import normalize_domain
from .ranking import LinearRanker, RankedHit, RankingSignals
from .routing import QueryPlan, QueryPlanner


@dataclass(frozen=True, slots=True)
class SearchResponse:
    query: str
    results: tuple[RankedHit, ...]
    plan: QueryPlan
    total_ms: float


class SearchService:
    def __init__(
        self,
        planner: QueryPlanner,
        indexes: Mapping[str, SearchIndex] | IndexProvider,
        *,
        ranker: LinearRanker | None = None,
        signals: dict[str, RankingSignals] | None = None,
        metrics: MetricsRegistry | None = None,
        feedback: LocalFeedbackAggregator | None = None,
        feedback_salt: bytes | None = None,
        per_shard_top_k: int = 1_000,
    ) -> None:
        self.planner = planner
        self.indexes: IndexProvider = InMemoryIndexProvider(indexes) if isinstance(indexes, Mapping) else indexes
        self.ranker = ranker or LinearRanker()
        self.signals = signals or {}
        self.metrics = metrics or MetricsRegistry()
        if (feedback is None) != (feedback_salt is None):
            raise ValueError("feedback and feedback_salt must be configured together")
        if per_shard_top_k <= 0:
            raise ValueError("per_shard_top_k must be positive")
        self.feedback = feedback
        self.feedback_salt = feedback_salt
        self.per_shard_top_k = per_shard_top_k

    def close(self) -> None:
        """Release provider-owned readers after the server has drained requests."""
        close = getattr(self.indexes, "close", None)
        if close is not None:
            close()

    def record_feedback(self, query: str, url_id: str, kind: FeedbackKind) -> None:
        """Record an opt-in local signal for a later query; immutable artifacts are untouched."""
        if self.feedback is None or self.feedback_salt is None:
            raise RuntimeError("runtime feedback is not configured")
        self.feedback.record(FeedbackEvent(classify_query(query, local_salt=self.feedback_salt), url_id, kind))

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        offset: int = 0,
        language: str | None = None,
        domain_filter: str | None = None,
        explain: bool = False,
        freshness: str | None = None,
        now: datetime | None = None,
    ) -> SearchResponse:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset must not be negative")
        started = perf_counter()
        checked_at = now or datetime.now(UTC)
        plan = self.planner.plan(query, language=language, domain_filter=domain_filter, freshness=freshness, now=checked_at)
        self.metrics.observe("planner_latency_ms", (perf_counter() - started) * 1000)
        effective_freshness = freshness or (plan.analysis.freshness if plan.analysis else None)
        windows = {"day": 1, "week": 7, "month": 30, "year": 365}
        if effective_freshness is not None and effective_freshness not in windows:
            raise ValueError("freshness must be day, week, month, or year")
        if checked_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = checked_at - timedelta(days=windows[effective_freshness]) if effective_freshness else None
        searchable = plan.analysis.searchable if plan.analysis else " ".join(
            part for part in query.split()
            if not part.casefold().startswith(("site:", "domain:", "freshness:"))
        )
        effective_domain_filter = domain_filter or (plan.analysis.domain_filter if plan.analysis else None)
        effective_domain_filter = normalize_domain(effective_domain_filter) if effective_domain_filter else None
        merged: dict[str, SearchHit] = {}
        requested = top_k + offset
        local_k = min(max(requested * 10, 100), self.per_shard_top_k)
        retrieval_started = perf_counter()
        for planned in plan.candidate_shards:
            index = self.indexes.get(planned.shard_id)
            if index is None:
                continue
            for hit in index.search(searchable, local_k):
                if cutoff is not None and not _within_freshness(hit.crawled_at, cutoff, checked_at):
                    continue
                existing = merged.get(hit.url_id.hex)
                if existing is None or hit.score > existing.score:
                    merged[hit.url_id.hex] = hit
        candidates = tuple(
            hit
            for hit in merged.values()
            if effective_domain_filter is None
            or (urlsplit(hit.url).hostname or "") == effective_domain_filter.casefold()
        )
        self.metrics.observe("retrieval_latency_ms", (perf_counter() - retrieval_started) * 1000)
        ranking_started = perf_counter()
        results = self.ranker.rank(candidates, self._signals_for(query, candidates), requested)[offset:]
        self.metrics.observe("ranking_latency_ms", (perf_counter() - ranking_started) * 1000)
        total_ms = (perf_counter() - started) * 1000
        self.metrics.increment("query_count")
        self.metrics.increment("shards_opened", len(plan.candidate_shards))
        self.metrics.observe("query_latency_ms", total_ms)
        self.metrics.observe("candidate_count", float(len(merged)))
        log_event(logging.getLogger(__name__), "query_completed", generation=self.planner.metadata.generation,
                  selected_shards=len(plan.candidate_shards), candidates=len(merged), results=len(results),
                  shard_ids=[shard.shard_id for shard in plan.candidate_shards],
                  duration_ms=total_ms, stop_reason=plan.stop_reason, budget_exhausted=plan.budget_exhausted)
        return SearchResponse(query, results, plan, total_ms)

    def _signals_for(self, query: str, candidates: tuple[SearchHit, ...]) -> dict[str, RankingSignals]:
        if self.feedback is None or self.feedback_salt is None:
            return self.signals
        query_class = classify_query(query, local_salt=self.feedback_salt)
        signals = dict(self.signals)
        for hit in candidates:
            base = signals.get(hit.url_id.hex, RankingSignals())
            signals[hit.url_id.hex] = replace(
                base,
                feedback=self.feedback.ranking_delta(query_class, hit.url_id.hex),
            )
        return signals

    def stats(self) -> dict[str, int]:
        metrics = self.metrics.snapshot()
        return {
            **self.indexes.stats(),
            "routing_shards": len(self.planner.metadata.shards),
            "query_count": metrics.counters.get("query_count", 0),
            "shards_opened": metrics.counters.get("shards_opened", 0),
        }


def _within_freshness(timestamp: str | None, cutoff: datetime, now: datetime) -> bool:
    if timestamp is None:
        return False
    try:
        observed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return False
    return observed.tzinfo is not None and cutoff <= observed <= now
