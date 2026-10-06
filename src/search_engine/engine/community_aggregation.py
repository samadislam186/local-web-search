"""Conservative aggregation for verified community contribution deltas."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .community import Contribution, verify_contribution
from .security import DetachedSignature, PinnedKeyVerifier


@dataclass(frozen=True, slots=True)
class CommunityAggregate:
    routing_hits: dict[str, int]
    ranking_deltas: dict[str, float]
    contributor_count: int


def aggregate_contributions(
    signed: list[tuple[Contribution, DetachedSignature]],
    verifier: PinnedKeyVerifier,
    *,
    minimum_contributors: int = 3,
    per_source_routing_cap: int = 10_000,
) -> CommunityAggregate:
    if minimum_contributors <= 0 or per_source_routing_cap <= 0:
        raise ValueError("aggregation limits must be positive")
    contributors: set[str] = set()
    routing: dict[str, int] = defaultdict(int)
    deltas: dict[str, list[float]] = defaultdict(list)
    for contribution, signature in signed:
        verify_contribution(contribution, signature, verifier)
        if contribution.contributor_id in contributors:
            raise ValueError("only one contribution per contributor is allowed per aggregate")
        contributors.add(contribution.contributor_id)
        for query_class, count in contribution.routing_hits.items():
            routing[query_class] += min(count, per_source_routing_cap)
        for url_id, delta in contribution.ranking_deltas.items():
            deltas[url_id].append(delta)
    if len(contributors) < minimum_contributors:
        return CommunityAggregate({}, {}, len(contributors))
    return CommunityAggregate(
        dict(routing),
        {url_id: float(median(values)) for url_id, values in deltas.items() if len(values) >= minimum_contributors},
        len(contributors),
    )
