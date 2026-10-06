"""Deterministic multi-signal candidate ranking and late domain diversity."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .index import SearchHit


@dataclass(frozen=True, slots=True)
class RankingSignals:
    authority: float = 0.0
    freshness: float = 0.0
    quality: float = 0.0
    duplicate_penalty: float = 0.0
    spam_penalty: float = 0.0
    feedback: float = 0.0


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: SearchHit
    score: float
    domain: str
    features: dict[str, float]


class LinearRanker:
    """Inspectable starting point; weights are hypotheses, not learned truth."""

    def __init__(self, *, max_per_domain: int = 2) -> None:
        if max_per_domain <= 0:
            raise ValueError("max_per_domain must be positive")
        self.max_per_domain = max_per_domain

    def rank(
        self, hits: tuple[SearchHit, ...], signals: dict[str, RankingSignals], top_k: int
    ) -> tuple[RankedHit, ...]:
        ranked: list[RankedHit] = []
        for hit in hits:
            feature = signals.get(hit.url_id.hex, RankingSignals())
            features = {
                "bm25": hit.score,
                "authority": feature.authority,
                "freshness": feature.freshness,
                "quality": feature.quality,
                "duplicate_penalty": feature.duplicate_penalty,
                "spam_penalty": feature.spam_penalty,
                "feedback": feature.feedback,
            }
            score = (
                features["bm25"]
                + 0.15 * features["authority"]
                + 0.10 * features["freshness"]
                + 0.10 * features["quality"]
                - 0.50 * features["duplicate_penalty"]
                - 1.00 * features["spam_penalty"]
                + features["feedback"]
            )
            ranked.append(RankedHit(hit, score, urlsplit(hit.url).hostname or "", features))
        selected: list[RankedHit] = []
        per_domain: dict[str, int] = {}
        for item in sorted(ranked, key=lambda item: (-item.score, item.hit.url_id.hex)):
            if per_domain.get(item.domain, 0) >= self.max_per_domain:
                continue
            selected.append(item)
            per_domain[item.domain] = per_domain.get(item.domain, 0) + 1
            if len(selected) == top_k:
                break
        return tuple(selected)
