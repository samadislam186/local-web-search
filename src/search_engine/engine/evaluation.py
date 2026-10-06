"""Deterministic retrieval, routing, and latency metrics for offline evaluation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import quantiles
from time import perf_counter
from typing import TypeVar

T = TypeVar("T")


def recall_at_k(returned: Iterable[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    if not relevant:
        return 0.0
    return len(set(list(returned)[:k]) & relevant) / len(relevant)


def precision_at_k(returned: Iterable[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    values = list(returned)[:k]
    return 0.0 if not values else len(set(values) & relevant) / len(values)


def reciprocal_rank(returned: Iterable[str], relevant: set[str]) -> float:
    return next((1.0 / rank for rank, value in enumerate(returned, start=1) if value in relevant), 0.0)


def ndcg_at_k(returned: Iterable[str], relevance: dict[str, float], k: int) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    values = list(returned)[:k]
    dcg = sum((2**relevance.get(value, 0.0) - 1) / _log2(rank + 1) for rank, value in enumerate(values, start=1))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_dcg = sum((2**score - 1) / _log2(rank + 1) for rank, score in enumerate(ideal, start=1))
    return 0.0 if ideal_dcg == 0 else dcg / ideal_dcg


def routing_containment(oracle_top_k: Iterable[str], selected_shards: set[str], document_shards: dict[str, str]) -> float:
    """Fraction of oracle results whose shard was selected by the planner."""
    oracle = list(oracle_top_k)
    if not oracle:
        return 1.0
    return sum(document_shards.get(document) in selected_shards for document in oracle) / len(oracle)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    runs: int
    total_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


def benchmark(operation: Callable[[], T], *, runs: int = 10) -> BenchmarkResult:
    if runs <= 0:
        raise ValueError("runs must be positive")
    timings: list[float] = []
    for _ in range(runs):
        started = perf_counter()
        operation()
        timings.append((perf_counter() - started) * 1000)
    ordered = sorted(timings)
    if runs == 1:
        p50 = p95 = p99 = ordered[0]
    else:
        percentiles = quantiles(ordered, n=100, method="inclusive")
        p50, p95, p99 = percentiles[49], percentiles[94], percentiles[98]
    return BenchmarkResult(runs, sum(timings), p50, p95, p99)


def _log2(value: int) -> float:
    # Input is positive by construction; avoids a heavier dependency.
    import math

    return math.log2(value)
