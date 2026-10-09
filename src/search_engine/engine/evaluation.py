"""Deterministic retrieval, routing, and latency metrics for offline evaluation."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from statistics import quantiles
from time import perf_counter, process_time
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
    rss_bytes: int | None = None
    disk_read_bytes: int | None = None
    cache_metrics: Mapping[str, int] | None = None
    cpu_ms: float = 0.0
    mmap_index_rss_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SearchBenchmarkResult:
    benchmark: BenchmarkResult
    routing_shards: int
    loaded_shards: int
    active_shard_bytes: int
    cache_hit_rate: float | None = None
    warmup_runs: int = 0

    def to_json(self) -> str:
        """Export a versioned report with the runtime context needed for comparison."""
        return json.dumps(
            {
                "report_version": 1,
                "environment": {"platform": platform.platform(), "python": platform.python_version()},
                "search": asdict(self),
            },
            sort_keys=True,
            allow_nan=False,
        )


@dataclass(frozen=True, slots=True)
class JudgedQuery:
    query: str
    relevance: Mapping[str, float]
    oracle_top_k: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelevanceReport:
    query_count: int
    top_k: int
    precision: float
    recall: float
    mrr: float
    ndcg: float
    containment: float | None

    def to_json(self) -> str:
        return json.dumps({"report_version": 1, "relevance": asdict(self)}, sort_keys=True, allow_nan=False)


def evaluate_search(
    service: object,
    judgments: Iterable[JudgedQuery],
    *,
    top_k: int = 10,
    document_shards: Mapping[str, str] | None = None,
) -> RelevanceReport:
    """Aggregate per-query metrics equally; relevance keys use stable UrlId hex strings."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    search = getattr(service, "search", None)
    if not callable(search):
        raise TypeError("service must expose a callable search method")
    precision = recall = mrr = ndcg = containment = 0.0
    count = containment_count = 0
    for judgment in judgments:
        response = search(judgment.query, top_k=top_k)
        returned = tuple(result.hit.url_id.hex for result in response.results)
        relevant = {url_id for url_id, grade in judgment.relevance.items() if grade > 0}
        precision += precision_at_k(returned, relevant, top_k)
        recall += recall_at_k(returned, relevant, top_k)
        mrr += reciprocal_rank(returned, relevant)
        ndcg += ndcg_at_k(returned, dict(judgment.relevance), top_k)
        if document_shards is not None and judgment.oracle_top_k:
            containment += routing_containment(
                judgment.oracle_top_k[:top_k],
                {shard.shard_id for shard in response.plan.candidate_shards},
                dict(document_shards),
            )
            containment_count += 1
        count += 1
    if count == 0:
        raise ValueError("at least one judged query is required")
    return RelevanceReport(
        count, top_k, precision / count, recall / count, mrr / count, ndcg / count,
        containment / containment_count if containment_count else None,
    )


def benchmark(
    operation: Callable[[], T],
    *,
    runs: int = 10,
    rss_reader: Callable[[], int | None] | None = None,
    disk_read_reader: Callable[[], int | None] | None = None,
    cache_snapshot: Callable[[], Mapping[str, int]] | None = None,
) -> BenchmarkResult:
    if runs <= 0:
        raise ValueError("runs must be positive")
    read_before = (disk_read_reader or _disk_read_bytes)()
    cpu_started = process_time()
    timings: list[float] = []
    for _ in range(runs):
        started = perf_counter()
        operation()
        timings.append((perf_counter() - started) * 1000)
    cpu_ms = (process_time() - cpu_started) * 1000
    ordered = sorted(timings)
    if runs == 1:
        p50 = p95 = p99 = ordered[0]
    else:
        percentiles = quantiles(ordered, n=100, method="inclusive")
        p50, p95, p99 = percentiles[49], percentiles[94], percentiles[98]
    rss_bytes = (rss_reader or _rss_bytes)()
    read_after = (disk_read_reader or _disk_read_bytes)()
    disk_read_bytes = None if read_before is None or read_after is None else max(0, read_after - read_before)
    metrics = dict(cache_snapshot()) if cache_snapshot is not None else None
    return BenchmarkResult(runs, sum(timings), p50, p95, p99, rss_bytes, disk_read_bytes, metrics, cpu_ms, _mmap_index_rss_bytes())


def benchmark_search(
    service: object, query: str, *, runs: int = 10, top_k: int = 10, warmup_runs: int = 0
) -> SearchBenchmarkResult:
    """Benchmark a read-only service through its public search/stats interfaces."""
    search = getattr(service, "search", None)
    stats = getattr(service, "stats", None)
    if not callable(search) or not callable(stats):
        raise TypeError("service must expose callable search and stats methods")
    if warmup_runs < 0 or runs <= 0:
        raise ValueError("runs must be positive and warmup_runs non-negative")
    for _ in range(warmup_runs):
        search(query, top_k=top_k)
    before = stats()
    result = benchmark(lambda: search(query, top_k=top_k), runs=runs, cache_snapshot=stats)
    snapshot = stats()
    hit_rate = None
    if "cache_hits" in snapshot and "cache_misses" in snapshot:
        hits = snapshot["cache_hits"] - before.get("cache_hits", 0)
        misses = snapshot["cache_misses"] - before.get("cache_misses", 0)
        hit_rate = hits / (hits + misses) if hits + misses else None
    return SearchBenchmarkResult(
        result,
        int(snapshot.get("routing_shards", 0)),
        int(snapshot.get("loaded_shards", 0)),
        int(snapshot.get("active_shard_bytes", 0)),
        hit_rate,
        warmup_runs,
    )


def benchmark_pagerank(graph: object, *, runs: int = 1, **pagerank_options: object) -> BenchmarkResult:
    """Benchmark an offline graph pass without making the evaluation layer own graph storage."""
    from .pagerank import compute_pagerank

    return benchmark(lambda: compute_pagerank(graph, **pagerank_options), runs=runs)


def _rss_bytes() -> int | None:
    """Return process RSS on Unix; callers can inject a platform-specific reader elsewhere."""
    measured = _process_measurement("rss")
    if measured is not None:
        return measured
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        return None
    # Linux reports KiB while macOS reports bytes.
    return value if sys.platform == "darwin" else value * 1024


def _disk_read_bytes() -> int | None:
    """Return Linux process read bytes; callers inject equivalent metrics elsewhere."""
    measured = _process_measurement("read_bytes")
    if measured is not None:
        return measured
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/io", encoding="utf-8") as handle:
            values = dict(line.split(":", 1) for line in handle)
        return int(values["read_bytes"].strip())
    except (OSError, KeyError, ValueError):
        return None


def _process_measurement(name: str) -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process()
        if name == "rss":
            return process.memory_info().rss
        return process.io_counters().read_bytes
    except (psutil.Error, AttributeError, NotImplementedError):
        return None


def _mmap_index_rss_bytes() -> int | None:
    """Resident file-backed bytes for extracted native index readers, not virtual mapping size."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return sum(item.rss for item in psutil.Process().memory_maps() if "search-tantivy-reader-" in item.path)
    except (psutil.Error, AttributeError, NotImplementedError):
        return None
def _log2(value: int) -> float:
    # Input is positive by construction; avoids a heavier dependency.
    import math

    return math.log2(value)
