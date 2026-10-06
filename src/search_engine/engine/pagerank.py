"""CPU-only offline PageRank over a generation-local hyperlink graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .graph import HyperlinkGraph


@dataclass(frozen=True, slots=True)
class PageRankResult:
    scores: tuple[float, ...]
    iterations: int
    converged: bool

    def by_url_id(self, graph: HyperlinkGraph) -> dict[str, float]:
        return {url_id.hex: self.scores[ordinal] for ordinal, url_id in enumerate(graph.ordinals.url_ids)}


def compute_pagerank(
    graph: HyperlinkGraph,
    *,
    damping: float = 0.85,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
    progress: Callable[[int, float], None] | None = None,
) -> PageRankResult:
    """Compute stationary authority with damping and dangling-node redistribution."""
    if not 0 < damping < 1 or tolerance <= 0 or max_iterations <= 0:
        raise ValueError("invalid PageRank parameters")
    node_count = len(graph.ordinals.url_ids)
    if node_count == 0:
        return PageRankResult((), 0, True)
    outgoing = tuple(graph.outgoing(node) for node in range(node_count))
    scores = [1.0 / node_count] * node_count
    base = (1.0 - damping) / node_count
    for iteration in range(1, max_iterations + 1):
        next_scores = [base] * node_count
        dangling = sum(scores[node] for node, targets in enumerate(outgoing) if not targets)
        dangling_share = damping * dangling / node_count
        for node in range(node_count):
            next_scores[node] += dangling_share
        for source, targets in enumerate(outgoing):
            if targets:
                share = damping * scores[source] / len(targets)
                for target in targets:
                    next_scores[target] += share
        delta = sum(abs(current - previous) for current, previous in zip(next_scores, scores, strict=True))
        scores = next_scores
        if progress is not None:
            progress(iteration, delta)
        if delta <= tolerance:
            return PageRankResult(tuple(scores), iteration, True)
    return PageRankResult(tuple(scores), max_iterations, False)
