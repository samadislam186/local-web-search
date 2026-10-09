"""Reproducible shard-routing experiment against an exhaustive lexical oracle."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from urllib.parse import urlsplit

from blake3 import blake3

from .evaluation import recall_at_k, routing_containment
from .index import Bm25fIndex, SearchDocument
from .routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata


@dataclass(frozen=True, slots=True)
class RoutingExperimentResult:
    assignment: str
    query_count: int
    mean_routing_recall: float
    mean_top_k_containment: float
    mean_shards_opened: float
    mean_bytes_opened: float
    p50_planning_ms: float
    p95_planning_ms: float
    p99_planning_ms: float


@dataclass(frozen=True, slots=True)
class AssignmentBalanceReport:
    shard_count: int
    document_counts: tuple[tuple[str, int], ...]
    min_documents: int
    max_documents: int


@dataclass(frozen=True, slots=True)
class RoutingStrategyDecision:
    outcome: str
    assignment: str | None
    max_shards: int | None
    containment: float | None
    rationale: str


@dataclass(frozen=True, slots=True)
class RoutingBudgetSweep:
    """Routing quality and cost measured across shard-count budgets."""

    assignment: str
    points: tuple[RoutingExperimentResult, ...]

    def to_dict(self, shard_budgets: tuple[int, ...]) -> dict[str, object]:
        """Return a stable JSON-ready benchmark record for an experiment run."""
        if len(shard_budgets) != len(self.points):
            raise ValueError("shard_budgets must align with points")
        return {
            "assignment": self.assignment,
            "budgets": [
                {
                    "max_shards": budget,
                    "query_count": point.query_count,
                    "routing_recall": point.mean_routing_recall,
                    "top_k_containment": point.mean_top_k_containment,
                    "mean_shards_opened": point.mean_shards_opened,
                    "mean_bytes_opened": point.mean_bytes_opened,
                    "p50_planning_ms": point.p50_planning_ms,
                    "p95_planning_ms": point.p95_planning_ms,
                    "p99_planning_ms": point.p99_planning_ms,
                }
                for budget, point in zip(shard_budgets, self.points, strict=True)
            ],
        }

    def write_json(self, path: Path, shard_budgets: tuple[int, ...]) -> None:
        """Atomically persist a JSON-ready benchmark record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(shard_budgets), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def hash_assignment(documents: tuple[SearchDocument, ...], shard_count: int) -> dict[str, tuple[SearchDocument, ...]]:
    return _partition(documents, shard_count, lambda document: document.url_id.value)


def domain_assignment(documents: tuple[SearchDocument, ...], shard_count: int) -> dict[str, tuple[SearchDocument, ...]]:
    return _partition(documents, shard_count, lambda document: (urlsplit(document.url).hostname or "").encode("utf-8"))


def hybrid_assignment(documents: tuple[SearchDocument, ...], shard_count: int) -> dict[str, tuple[SearchDocument, ...]]:
    """Experimental domain plus lexical-signature assignment for routing comparison."""
    return _partition(documents, shard_count, lambda document: _hybrid_key(document).encode("utf-8"))


def assignment_balance_report(segments: dict[str, tuple[SearchDocument, ...]]) -> AssignmentBalanceReport:
    """Return stable per-shard document counts for assignment comparison."""
    if not segments:
        raise ValueError("segments must not be empty")
    counts = tuple((shard_id, len(documents)) for shard_id, documents in sorted(segments.items()))
    values = tuple(count for _shard_id, count in counts)
    return AssignmentBalanceReport(len(counts), counts, min(values), max(values))


def compare_assignments(
    documents: tuple[SearchDocument, ...], queries: tuple[str, ...], *, shard_count: int, top_k: int, max_shards: int
) -> tuple[RoutingExperimentResult, ...]:
    assignments = (
        ("hash", hash_assignment(documents, shard_count)),
        ("domain", domain_assignment(documents, shard_count)),
        ("hybrid", hybrid_assignment(documents, shard_count)),
    )
    return tuple(
        run_experiment(name, segments, queries, top_k=top_k, max_shards=max_shards)
        for name, segments in assignments
    )


def compare_assignment_sweeps(
    documents: tuple[SearchDocument, ...],
    queries: tuple[str, ...],
    *,
    shard_count: int,
    top_k: int = 10,
    shard_budgets: tuple[int, ...] = (1, 2, 5, 10),
    byte_budget: int = 1 << 30,
) -> dict[str, RoutingBudgetSweep]:
    """Measure every assignment candidate under the same shard-budget sweep."""
    assignments = {
        "hash": hash_assignment(documents, shard_count),
        "domain": domain_assignment(documents, shard_count),
        "hybrid": hybrid_assignment(documents, shard_count),
    }
    return {
        name: sweep_shard_budgets(
            name,
            segments,
            queries,
            top_k=top_k,
            shard_budgets=shard_budgets,
            byte_budget=byte_budget,
        )
        for name, segments in assignments.items()
    }


def assignment_sweep_report(
    sweeps: dict[str, RoutingBudgetSweep], shard_budgets: tuple[int, ...]
) -> dict[str, object]:
    """Return a stable machine-readable comparison suitable for a go/no-go record."""
    if not sweeps:
        raise ValueError("at least one assignment sweep is required")
    return {
        "format_version": 1,
        "shard_budgets": shard_budgets,
        "assignments": {name: sweeps[name].to_dict(shard_budgets) for name in sorted(sweeps)},
    }


def select_routing_strategy(
    sweeps: dict[str, RoutingBudgetSweep],
    shard_budgets: tuple[int, ...],
    *,
    minimum_containment: float,
) -> RoutingStrategyDecision:
    """Choose the cheapest measured strategy meeting an explicit containment target."""
    if not 0 < minimum_containment <= 1:
        raise ValueError("minimum_containment must be in (0, 1]")
    candidates: list[tuple[float, int, str, RoutingExperimentResult]] = []
    for assignment, sweep in sweeps.items():
        if len(sweep.points) != len(shard_budgets):
            raise ValueError("sweep points must align with shard budgets")
        for budget, point in zip(shard_budgets, sweep.points, strict=True):
            if point.mean_top_k_containment >= minimum_containment:
                candidates.append((point.mean_bytes_opened, budget, assignment, point))
    if not candidates:
        return RoutingStrategyDecision(
            "revise",
            None,
            None,
            None,
            f"no strategy met containment target {minimum_containment:.3f}; revise routing architecture",
        )
    _bytes, budget, assignment, point = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return RoutingStrategyDecision(
        "select",
        assignment,
        budget,
        point.mean_top_k_containment,
        f"measured containment {point.mean_top_k_containment:.3f} meets target {minimum_containment:.3f}",
    )


def sweep_shard_budgets(
    assignment_name: str,
    segments: dict[str, tuple[SearchDocument, ...]],
    queries: tuple[str, ...],
    *,
    top_k: int = 10,
    shard_budgets: tuple[int, ...] = (1, 2, 5, 10),
    byte_budget: int = 1 << 30,
) -> RoutingBudgetSweep:
    """Run one assignment through explicit, comparable shard-count budgets."""
    if not shard_budgets or any(budget <= 0 for budget in shard_budgets):
        raise ValueError("shard_budgets must contain positive values")
    return RoutingBudgetSweep(
        assignment_name,
        tuple(
            run_experiment(
                assignment_name,
                segments,
                queries,
                top_k=top_k,
                max_shards=budget,
                byte_budget=byte_budget,
            )
            for budget in shard_budgets
        ),
    )


def run_experiment(
    assignment_name: str,
    segments: dict[str, tuple[SearchDocument, ...]],
    queries: tuple[str, ...],
    *,
    top_k: int = 10,
    max_shards: int = 10,
    byte_budget: int = 1 << 30,
) -> RoutingExperimentResult:
    if not segments or not queries:
        raise ValueError("segments and queries must not be empty")
    document_shards = {document.url_id.hex: shard_id for shard_id, documents in segments.items() for document in documents}
    routing = RoutingMetadata(
        "experiment",
        tuple(
            ShardRoutingMetadata.from_documents(
                shard_id,
                "experiment",
                max(1, len(Bm25fIndex(documents).to_payload())),
                documents,
            )
            for shard_id, documents in sorted(segments.items())
        ),
    )
    planner = QueryPlanner(routing, max_shards=max_shards, byte_budget=byte_budget)
    recalls: list[float] = []
    containments: list[float] = []
    shard_counts: list[int] = []
    byte_counts: list[int] = []
    planning_times_ms: list[float] = []
    for query in queries:
        oracle_ids = tuple(hit.url_id.hex for hit in exhaustive_oracle(segments, query, top_k))
        started = perf_counter()
        plan = planner.plan(query)
        planning_times_ms.append((perf_counter() - started) * 1000)
        selected = {item.shard_id for item in plan.candidate_shards}
        routed_ids = tuple(document_id for document_id in oracle_ids if document_shards[document_id] in selected)
        recalls.append(recall_at_k(routed_ids, set(oracle_ids), top_k))
        containments.append(routing_containment(oracle_ids, selected, document_shards))
        shard_counts.append(len(plan.candidate_shards))
        byte_counts.append(sum(item.byte_size for item in plan.candidate_shards))
    count = len(queries)
    p50, p95, p99 = _latency_percentiles(planning_times_ms)
    return RoutingExperimentResult(
        assignment_name,
        count,
        sum(recalls) / count,
        sum(containments) / count,
        sum(shard_counts) / count,
        sum(byte_counts) / count,
        p50,
        p95,
        p99,
    )


def exhaustive_oracle(
    segments: dict[str, tuple[SearchDocument, ...]], query: str, top_k: int
) -> tuple[SearchDocument, ...]:
    """Return the deterministic all-shard lexical ground truth for one query."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    all_documents = tuple(document for shard_id in sorted(segments) for document in segments[shard_id])
    hits = Bm25fIndex(all_documents).search(query, top_k)
    by_id = {document.url_id: document for document in all_documents}
    return tuple(by_id[hit.url_id] for hit in hits)


def _partition(
    documents: tuple[SearchDocument, ...], shard_count: int, key: callable
) -> dict[str, tuple[SearchDocument, ...]]:
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    partitions: dict[str, list[SearchDocument]] = defaultdict(list)
    for document in documents:
        shard = int.from_bytes(blake3(key(document)).digest(length=8), "big") % shard_count
        partitions[f"shard-{shard:04d}"].append(document)
    return {shard_id: tuple(items) for shard_id, items in partitions.items()}


def _terms(documents: tuple[SearchDocument, ...]) -> tuple[str, ...]:
    return tuple(
        term
        for document in documents
        for term in f"{document.url} {document.title} {document.body} {document.headings} {document.anchor}".split()
    )


def _hybrid_key(document: SearchDocument) -> str:
    host = urlsplit(document.url).hostname or ""
    signature = " ".join(sorted(set(f"{document.title} {document.headings} {document.body}".casefold().split()))[:4])
    return f"{host}|{signature}"


def _latency_percentiles(samples: list[float]) -> tuple[float, float, float]:
    if len(samples) == 1:
        return samples[0], samples[0], samples[0]
    values = quantiles(samples, n=100, method="inclusive")
    return values[49], values[94], values[98]
