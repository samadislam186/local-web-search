"""Deterministic multi-signal candidate ranking and late domain diversity."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from urllib.parse import urlsplit

from .index import SearchHit

RANKING_FEATURE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RankingSignals:
    """Versioned, bounded features supplied by immutable build artifacts or opt-in runtime feedback."""

    schema_version: int = field(default=RANKING_FEATURE_SCHEMA_VERSION, kw_only=True)
    authority: float = 0.0
    freshness: float = 0.0
    quality: float = 0.0
    duplicate_penalty: float = 0.0
    spam_penalty: float = 0.0
    feedback: float = 0.0
    title_match: float = 0.0
    phrase_match: float = 0.0
    url_match: float = 0.0
    anchor_match: float = 0.0

    def __post_init__(self) -> None:
        if self.schema_version != RANKING_FEATURE_SCHEMA_VERSION:
            raise ValueError("unsupported ranking feature schema")
        if not all(isfinite(value) for value in self.values().values()):
            raise ValueError("ranking features must be finite")

    def values(self) -> dict[str, float]:
        return {
            "title_match": self.title_match,
            "phrase_match": self.phrase_match,
            "url_match": self.url_match,
            "anchor_match": self.anchor_match,
            "authority": self.authority,
            "freshness": self.freshness,
            "quality": self.quality,
            "duplicate_penalty": self.duplicate_penalty,
            "spam_penalty": self.spam_penalty,
            "feedback": self.feedback,
        }


@dataclass(frozen=True, slots=True)
class LinearRankingWeights:
    """Stable baseline weights; changing them requires a feature-schema review."""

    bm25: float = 1.0
    title_match: float = 0.20
    phrase_match: float = 0.20
    url_match: float = 0.10
    anchor_match: float = 0.10
    authority: float = 0.15
    freshness: float = 0.10
    quality: float = 0.10
    duplicate_penalty: float = -0.50
    spam_penalty: float = -1.00
    feedback: float = 1.0


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: SearchHit
    score: float
    domain: str
    features: dict[str, float]
    contributions: dict[str, float]


class LinearRanker:
    """Inspectable starting point; weights are hypotheses, not learned truth."""

    def __init__(self, *, max_per_domain: int = 2, weights: LinearRankingWeights | None = None) -> None:
        if max_per_domain <= 0:
            raise ValueError("max_per_domain must be positive")
        self.max_per_domain = max_per_domain
        self.weights = weights or LinearRankingWeights()

    def rank(
        self, hits: tuple[SearchHit, ...], signals: dict[str, RankingSignals], top_k: int
    ) -> tuple[RankedHit, ...]:
        ranked: list[RankedHit] = []
        for hit in hits:
            feature = signals.get(hit.url_id.hex, RankingSignals())
            features = {
                "bm25": hit.score,
                **feature.values(),
            }
            contributions = {name: getattr(self.weights, name) * value for name, value in features.items()}
            score = sum(contributions.values())
            ranked.append(RankedHit(hit, score, urlsplit(hit.url).hostname or "", features, contributions))
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
