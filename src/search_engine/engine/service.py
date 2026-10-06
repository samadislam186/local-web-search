"""Read-only serving orchestration: plan, bounded local retrieval, merge, rank."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from time import perf_counter
from urllib.parse import urlsplit

from .feedback import FeedbackEvent, FeedbackKind, LocalFeedbackAggregator, classify_query
from .index import SearchHit, SearchIndex
from .index_provider import IndexProvider, InMemoryIndexProvider
from .observability import MetricsRegistry
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
    ) -> SearchResponse:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset must not be negative")
        started = perf_counter()
        plan = self.planner.plan(query, language=language)
        merged: dict[str, SearchHit] = {}
        requested = top_k + offset
        local_k = min(max(requested * 10, 100), self.per_shard_top_k)
        for planned in plan.candidate_shards:
            index = self.indexes.get(planned.shard_id)
            if index is None:
                continue
            for hit in index.search(query, local_k):
                existing = merged.get(hit.url_id.hex)
                if existing is None or hit.score > existing.score:
                    merged[hit.url_id.hex] = hit
        candidates = tuple(
            hit
            for hit in merged.values()
            if domain_filter is None or (urlsplit(hit.url).hostname or "") == domain_filter.casefold()
        )
        results = self.ranker.rank(candidates, self._signals_for(query, candidates), requested)[offset:]
        total_ms = (perf_counter() - started) * 1000
        self.metrics.increment("query_count")
        self.metrics.increment("shards_opened", len(plan.candidate_shards))
        self.metrics.observe("query_latency_ms", total_ms)
        self.metrics.observe("candidate_count", float(len(merged)))
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
