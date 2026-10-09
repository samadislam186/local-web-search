"""Immutable routing metadata and bounded, explainable shard planning."""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from blake3 import blake3

from .index import SearchDocument
from .query_analysis import QueryAnalysis, analyze_query, normalize_domain
from .query_terms import TOKEN, one_edit_variants
from .storage import ArtifactValidationError

ROUTING_METADATA_VERSION = 2
_TOKEN = TOKEN


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
    hosts: tuple[str, ...] = ()
    title_terms: tuple[str, ...] = ()
    title_ngrams: tuple[str, ...] = ()
    term_document_frequencies: dict[str, int] = field(default_factory=dict)
    term_min_url_ids: dict[str, str] = field(default_factory=dict)
    summary_version: int = 1
    crawled_at_min: str | None = None
    crawled_at_max: str | None = None

    def __post_init__(self) -> None:
        term_set = set(self.terms)
        if (self.crawled_at_min is None) != (self.crawled_at_max is None):
            raise ValueError("crawl-time range must be complete")
        if self.crawled_at_min is not None:
            lower, upper = datetime.fromisoformat(self.crawled_at_min), datetime.fromisoformat(self.crawled_at_max)
            if lower.tzinfo is None or upper.tzinfo is None or lower > upper:
                raise ValueError("invalid crawl-time range")
        if self.summary_version not in {1, 2}:
            raise ValueError("unsupported routing summary version")
        if (self.hosts != tuple(sorted(set(self.hosts))) or self.title_terms != tuple(sorted(set(self.title_terms)))
                or self.title_ngrams != tuple(sorted(set(self.title_ngrams)))):
            raise ValueError("routing host/title summaries must be sorted and unique")
        if not set(self.title_terms).issubset(term_set) or any(
            term not in term_set or not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= self.document_count
            for term, count in self.term_document_frequencies.items()
        ):
            raise ValueError("invalid routing term statistics")
        if any(term not in term_set or not isinstance(identifier, str)
               or len(identifier) != 32 or any(character not in "0123456789abcdef" for character in identifier)
               for term, identifier in self.term_min_url_ids.items()):
            raise ValueError("invalid routing document tie-break IDs")
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

    @classmethod
    def from_documents(cls, shard_id: str, generation: str, byte_size: int, documents: tuple[SearchDocument, ...]):
        frequencies: dict[str, int] = {}
        minimum_ids: dict[str, str] = {}
        titles: set[str] = set()
        ngrams: set[str] = set()
        hosts: set[str] = set()
        timestamps = []
        for document in documents:
            identifier = document.url_id.hex
            try:
                observed = datetime.fromisoformat(document.crawled_at)
                if observed.tzinfo is not None:
                    timestamps.append(observed.astimezone(UTC))
            except (TypeError, ValueError):
                pass
            host = document.host or (urlsplit(document.url).hostname or "")
            if host:
                hosts.add(host.casefold())
            titles.update(_terms(f"{document.title} {document.headings}"))
            for value in (document.title, document.headings):
                words = [match.group(0).casefold() for match in TOKEN.finditer(value)]
                for size in range(2, 6):
                    ngrams.update(" ".join(words[start:start + size]) for start in range(len(words) - size + 1))
            values = f"{document.url} {document.title} {document.headings} {document.body} {document.anchor} {host} {document.domain}"
            for term in _terms(values):
                frequencies[term] = frequencies.get(term, 0) + 1
                minimum_ids[term] = min(identifier, minimum_ids.get(term, identifier))
        terms = tuple(sorted(frequencies))
        return cls(shard_id, generation, len(documents), byte_size, terms,
                   BloomFilter.build(terms, bit_count=max(4096, len(terms) * 12), hash_count=8),
                   hosts=tuple(sorted(hosts)), title_terms=tuple(sorted(titles)),
                   title_ngrams=tuple(sorted(ngrams)),
                   term_document_frequencies=frequencies, summary_version=2,
                   term_min_url_ids=minimum_ids,
                   crawled_at_min=min(timestamps).isoformat() if timestamps else None,
                   crawled_at_max=max(timestamps).isoformat() if timestamps else None,
                   term_min=terms[0] if terms else None, term_max=terms[-1] if terms else None)

    def has_exact_term(self, term: str) -> bool:
        """Binary-search the immutable sorted term summary without Bloom uncertainty."""
        position = bisect_left(self.terms, term)
        return position < len(self.terms) and self.terms[position] == term

    def has_title_term(self, term: str) -> bool:
        position = bisect_left(self.title_terms, term)
        return position < len(self.title_terms) and self.title_terms[position] == term


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
                        "hosts": tuple(item.get("hosts", [])),
                        "title_terms": tuple(item.get("title_terms", [])),
                        "title_ngrams": tuple(item.get("title_ngrams", [])),
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
        if metadata.format_version not in {1, ROUTING_METADATA_VERSION}:
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
    matching_url_id: str | None = None


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
        self._open_estimates = {shard.shard_id: shard.estimated_open_ms for shard in metadata.shards}

    def plan(self, query: str, *, language: str | None = None, domain_filter: str | None = None,
             freshness: str | None = None, now: datetime | None = None) -> QueryPlan:
        analysis = analyze_query(query)
        domain_filter = (domain_filter or analysis.domain_filter)
        domain_filter = normalize_domain(domain_filter) if domain_filter else None
        terms = analysis.tokens
        variants = {term: one_edit_variants(term) for term in analysis.fuzzy_terms}
        window = freshness or analysis.freshness
        windows = {"day": 1, "week": 7, "month": 30, "year": 365}
        if window is not None and window not in windows:
            raise ValueError("freshness must be day, week, month, or year")
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = checked_at - timedelta(days=windows[window]) if window else None
        eligible = tuple(shard for shard in self.metadata.shards
                         if not (language and shard.languages and language not in shard.languages)
                         and not (domain_filter and shard.hosts and domain_filter not in shard.hosts)
                         and not (cutoff and shard.summary_version >= 2 and (
                             shard.crawled_at_max is None
                             or datetime.fromisoformat(shard.crawled_at_max) < cutoff
                             or datetime.fromisoformat(shard.crawled_at_min) > checked_at)))
        weights = {term: 1 + math.log((len(eligible) + 1) / (1 + sum(shard.has_exact_term(term) for shard in eligible)))
                   for term in terms}
        planned: list[PlannedShard] = []
        for shard in eligible:
            exact = tuple(term for term in terms if shard.has_exact_term(term))
            bloom_only = tuple(term for term in terms if term not in exact and shard.may_have_term(term))
            fuzzy_matches = tuple((term, candidate) for term, values in variants.items()
                                  for candidate in values if candidate != term and shard.has_exact_term(candidate))
            if not exact and not bloom_only and not fuzzy_matches and not (domain_filter and not terms):
                continue
            score = 2.0 * len(exact) + len(bloom_only) + min(max(shard.hotness, 0.0), 1.0) * 0.01
            reasons = tuple(f"term:{term}" for term in exact) + tuple(f"bloom:{term}" for term in bloom_only)
            if shard.summary_version >= 2:
                score = 2 * sum(weights[term] for term in exact) + 0.25 * len(bloom_only)
                title_hits = sum(shard.has_title_term(term) for term in exact)
                score += 0.1 * title_hits
                score += sum(0.1 * (1 - shard.term_document_frequencies.get(term, shard.document_count) / max(1, shard.document_count))
                             for term in exact)
                if title_hits:
                    reasons += ("title_summary",)
                for phrase in analysis.phrases:
                    normalized_phrase = " ".join(match.group(0).casefold() for match in TOKEN.finditer(phrase))
                    position = bisect_left(shard.title_ngrams, normalized_phrase)
                    if position < len(shard.title_ngrams) and shard.title_ngrams[position] == normalized_phrase:
                        score += 2.0
                        reasons += ("title_phrase",)
                if exact:
                    reasons += ("rarity_weighted",)
                if analysis.navigation_host and analysis.navigation_host in shard.hosts:
                    score += 4.0
                    reasons += ("navigation_host",)
            if domain_filter:
                score += 1.0
                reasons += ("domain_filter",)
            if fuzzy_matches:
                score += 1.5 * len({term for term, _candidate in fuzzy_matches})
                reasons += tuple(f"fuzzy:{term}:{candidate}" for term, candidate in fuzzy_matches[:64])
            hints = [shard.term_min_url_ids[term] for term in exact if term in shard.term_min_url_ids]
            hints.extend(shard.term_min_url_ids[candidate] for _term, candidate in fuzzy_matches if candidate in shard.term_min_url_ids)
            planned.append(PlannedShard(shard.shard_id, score, reasons, shard.byte_size, min(hints) if hints else None))
        chosen: list[PlannedShard] = []
        opened = 0
        estimated_latency_ms = 0.0
        stopped_early = False
        stop_reason: str | None = None
        budget_exhausted = False
        ordered = sorted(planned, key=lambda item: (-item.score, item.matching_url_id or "f" * 32, item.shard_id))
        total_score = sum(shard.score for shard in ordered)
        selected_score = 0.0
        remaining_score = total_score
        for shard in ordered:
            remaining_score -= shard.score
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
        return self._open_estimates[planned.shard_id]


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(value)))
