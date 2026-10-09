"""Immutable routing metadata and bounded, explainable shard planning."""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from blake3 import blake3

from .query_analysis import QueryAnalysis, analyze_query
from .storage import ArtifactValidationError

ROUTING_METADATA_VERSION = 1
_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)


@dataclass(frozen=True, slots=True)
class BloomFilter:
    bit_count: int
    hash_count: int
    bits: bytes

    @classmethod
    def build(cls, values: Iterable[str], *, bit_count: int = 4096, hash_count: int = 5) -> BloomFilter:
        if bit_count <= 0 or hash_count <= 0:
            raise ValueError("Bloom dimensions must be positive")
        bits = bytearray(math.ceil(bit_count / 8))
        result = cls(bit_count, hash_count, bytes(bits))
        for value in values:
            bits = bytearray(result.bits)
            for position in result._positions(value):
                bits[position // 8] |= 1 << (position % 8)
            result = cls(bit_count, hash_count, bytes(bits))
        return result

    def may_contain(self, value: str) -> bool:
        if self.bit_count <= 0 or self.hash_count <= 0 or len(self.bits) != math.ceil(self.bit_count / 8):
            raise ArtifactValidationError("invalid Bloom filter dimensions")
        return all(self.bits[position // 8] & (1 << (position % 8)) for position in self._positions(value))

    def _positions(self, value: str) -> tuple[int, ...]:
        digest = blake3(value.encode("utf-8")).digest(length=self.hash_count * 4)
        return tuple(int.from_bytes(digest[offset : offset + 4], "big") % self.bit_count for offset in range(0, len(digest), 4))


@dataclass(frozen=True, slots=True)
class ShardRoutingMetadata:
    shard_id: str
    generation: str
    document_count: int
    byte_size: int
    terms: tuple[str, ...]
    bloom: BloomFilter
    languages: tuple[str, ...] = ()
    hotness: float = 0.0
    term_min: str | None = None
    term_max: str | None = None
    estimated_open_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.document_count < 0 or self.byte_size <= 0 or self.estimated_open_ms < 0:
            raise ValueError("routing shard document_count must be non-negative and byte_size positive")
        if self.terms != tuple(sorted(set(self.terms))):
            raise ValueError("routing terms must be sorted and unique")
        if (self.term_min is None) != (self.term_max is None) or (
            self.term_min is not None and self.term_max is not None and self.term_min > self.term_max
        ):
            raise ValueError("routing shard term range must be complete and ordered")
        if self.terms and self.term_min is not None and (
            self.term_min != self.terms[0] or self.term_max != self.terms[-1]
        ):
            raise ValueError("routing range must cover the complete term summary")

    @classmethod
    def from_terms(
        cls,
        shard_id: str,
        generation: str,
        document_count: int,
        byte_size: int,
        terms: Iterable[str],
        *,
        languages: Iterable[str] = (),
        hotness: float = 0.0,
        estimated_open_ms: float = 0.0,
    ) -> ShardRoutingMetadata:
        normalized = tuple(sorted(set(_terms(" ".join(terms)))))
        return cls(
            shard_id,
            generation,
            document_count,
            byte_size,
            normalized,
            BloomFilter.build(normalized, bit_count=max(4096, len(normalized) * 12), hash_count=8),
            tuple(sorted(set(languages))),
            hotness,
            normalized[0] if normalized else None,
            normalized[-1] if normalized else None,
            estimated_open_ms,
        )

    def may_have_term(self, term: str) -> bool:
        """Use a zero-false-negative lexical range guard before the Bloom lookup."""
        if self.term_min is not None and (term < self.term_min or term > self.term_max):
            return False
        return self.has_exact_term(term) or self.bloom.may_contain(term)

    def has_exact_term(self, term: str) -> bool:
        """Binary-search the immutable sorted term summary without Bloom uncertainty."""
        position = bisect_left(self.terms, term)
        return position < len(self.terms) and self.terms[position] == term


@dataclass(frozen=True, slots=True)
class RoutingMetadata:
    generation: str
    shards: tuple[ShardRoutingMetadata, ...]
    format_version: int = ROUTING_METADATA_VERSION

    def to_bytes(self) -> bytes:
        payload = {
            "generation": self.generation,
            "format_version": self.format_version,
            "shards": [
                {
                    **asdict(shard),
                    "bloom": {
                        "bit_count": shard.bloom.bit_count,
                        "hash_count": shard.bloom.hash_count,
                        "bits": shard.bloom.bits.hex(),
                    },
                }
                for shard in self.shards
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> RoutingMetadata:
        try:
            data = json.loads(payload)
            shards = tuple(
                ShardRoutingMetadata(
                    **{
                        **item,
                        "terms": tuple(item["terms"]),
                        "languages": tuple(item.get("languages", [])),
                        "bloom": BloomFilter(
                            int(item["bloom"]["bit_count"]),
                            int(item["bloom"]["hash_count"]),
                            bytes.fromhex(item["bloom"]["bits"]),
                        ),
                    }
                )
                for item in data["shards"]
            )
            metadata = cls(str(data["generation"]), shards, int(data["format_version"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("invalid routing metadata") from exc
        if metadata.format_version != ROUTING_METADATA_VERSION:
            raise ArtifactValidationError("unsupported routing metadata format")
        if any(shard.generation != metadata.generation for shard in metadata.shards):
            raise ArtifactValidationError("routing metadata generation mismatch")
        return metadata


@dataclass(frozen=True, slots=True)
class PlannedShard:
    shard_id: str
    score: float
    reasons: tuple[str, ...]
    byte_size: int


@dataclass(frozen=True, slots=True)
class QueryPlan:
    query: str
    candidate_shards: tuple[PlannedShard, ...]
    max_shards: int
    byte_budget: int
    stopped_early: bool = False
    analysis: QueryAnalysis | None = None
    estimated_latency_ms: float = 0.0
    stop_reason: str | None = None
    budget_exhausted: bool = False


class QueryPlanner:
    def __init__(
        self,
        metadata: RoutingMetadata,
        *,
        max_shards: int = 10,
        byte_budget: int = 1 << 30,
        latency_budget_ms: float | None = None,
        minimum_score: float = 0.0,
        minimum_marginal_gain: float | None = None,
        confidence_target: float | None = None,
        maximum_remaining_score: float | None = None,
    ) -> None:
        if (
            max_shards <= 0
            or byte_budget <= 0
            or minimum_score < 0
            or (minimum_marginal_gain is not None and minimum_marginal_gain < 0)
            or (confidence_target is not None and not 0 < confidence_target <= 1)
            or (maximum_remaining_score is not None and maximum_remaining_score < 0)
            or (latency_budget_ms is not None and latency_budget_ms <= 0)
        ):
            raise ValueError("max_shards and byte_budget must be positive; minimum_score must be non-negative")
        self.metadata = metadata
        self.max_shards = max_shards
        self.byte_budget = byte_budget
        self.latency_budget_ms = latency_budget_ms
        self.minimum_score = minimum_score
        self.minimum_marginal_gain = minimum_marginal_gain
        self.confidence_target = confidence_target
        self.maximum_remaining_score = maximum_remaining_score

    def plan(self, query: str, *, language: str | None = None) -> QueryPlan:
        analysis = analyze_query(query)
        terms = analysis.tokens
        planned: list[PlannedShard] = []
        for shard in self.metadata.shards:
            if language and shard.languages and language not in shard.languages:
                continue
            exact = tuple(term for term in terms if shard.has_exact_term(term))
            bloom_only = tuple(term for term in terms if term not in exact and shard.may_have_term(term))
            if not exact and not bloom_only:
                continue
            score = 2.0 * len(exact) + len(bloom_only) + min(max(shard.hotness, 0.0), 1.0) * 0.01
            reasons = tuple(f"term:{term}" for term in exact) + tuple(f"bloom:{term}" for term in bloom_only)
            planned.append(PlannedShard(shard.shard_id, score, reasons, shard.byte_size))
        chosen: list[PlannedShard] = []
        opened = 0
        estimated_latency_ms = 0.0
        stopped_early = False
        stop_reason: str | None = None
        budget_exhausted = False
        ordered = sorted(planned, key=lambda item: (-item.score, item.shard_id))
        total_score = sum(shard.score for shard in ordered)
        selected_score = 0.0
        for position, shard in enumerate(ordered):
            if shard.score < self.minimum_score:
                stopped_early = True
                stop_reason = "minimum_score"
                break
            if self.minimum_marginal_gain is not None and shard.score < self.minimum_marginal_gain:
                stopped_early = True
                stop_reason = "marginal_gain"
                break
            if (
                len(chosen) == self.max_shards
                or opened + shard.byte_size > self.byte_budget
                or (
                    self.latency_budget_ms is not None
                    and estimated_latency_ms + self._estimated_open_ms(shard) > self.latency_budget_ms
                )
            ):
                budget_exhausted = True
                continue
            chosen.append(shard)
            opened += shard.byte_size
            estimated_latency_ms += self._estimated_open_ms(shard)
            selected_score += shard.score
            remaining_score = sum(item.score for item in ordered[position + 1 :])
            if self.confidence_target is not None and selected_score / total_score >= self.confidence_target:
                stopped_early = True
                stop_reason = "confidence"
                break
            if self.maximum_remaining_score is not None and remaining_score <= self.maximum_remaining_score:
                stopped_early = True
                stop_reason = "remaining_score"
                break
        return QueryPlan(
            analysis.normalized,
            tuple(chosen),
            self.max_shards,
            self.byte_budget,
            stopped_early,
            analysis,
            estimated_latency_ms,
            stop_reason,
            budget_exhausted,
        )

    def _estimated_open_ms(self, planned: PlannedShard) -> float:
        return next(shard.estimated_open_ms for shard in self.metadata.shards if shard.shard_id == planned.shard_id)


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(value)))
