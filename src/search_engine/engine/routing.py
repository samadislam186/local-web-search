"""Immutable routing metadata and bounded, explainable shard planning."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from blake3 import blake3

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

    def __post_init__(self) -> None:
        if self.document_count < 0 or self.byte_size <= 0:
            raise ValueError("routing shard document_count must be non-negative and byte_size positive")

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
    ) -> ShardRoutingMetadata:
        normalized = tuple(sorted(set(_terms(" ".join(terms)))))
        return cls(shard_id, generation, document_count, byte_size, normalized, BloomFilter.build(normalized), tuple(sorted(set(languages))), hotness)


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


class QueryPlanner:
    def __init__(
        self, metadata: RoutingMetadata, *, max_shards: int = 10, byte_budget: int = 1 << 30, minimum_score: float = 0.0
    ) -> None:
        if max_shards <= 0 or byte_budget <= 0 or minimum_score < 0:
            raise ValueError("max_shards and byte_budget must be positive; minimum_score must be non-negative")
        self.metadata = metadata
        self.max_shards = max_shards
        self.byte_budget = byte_budget
        self.minimum_score = minimum_score

    def plan(self, query: str, *, language: str | None = None) -> QueryPlan:
        terms = _terms(query)
        planned: list[PlannedShard] = []
        for shard in self.metadata.shards:
            if language and shard.languages and language not in shard.languages:
                continue
            exact = tuple(term for term in terms if term in shard.terms)
            bloom_only = tuple(term for term in terms if term not in exact and shard.bloom.may_contain(term))
            if not exact and not bloom_only:
                continue
            score = 2.0 * len(exact) + len(bloom_only) + min(max(shard.hotness, 0.0), 1.0) * 0.01
            reasons = tuple(f"term:{term}" for term in exact) + tuple(f"bloom:{term}" for term in bloom_only)
            planned.append(PlannedShard(shard.shard_id, score, reasons, shard.byte_size))
        chosen: list[PlannedShard] = []
        opened = 0
        stopped_early = False
        for shard in sorted(planned, key=lambda item: (-item.score, item.shard_id)):
            if shard.score < self.minimum_score:
                stopped_early = True
                break
            if len(chosen) == self.max_shards or opened + shard.byte_size > self.byte_budget:
                continue
            chosen.append(shard)
            opened += shard.byte_size
        return QueryPlan(query, tuple(chosen), self.max_shards, self.byte_budget, stopped_early)


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(value)))
