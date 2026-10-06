"""Project-owned lexical index contract and CPU-only BM25F reference segment.

The reference segment is for the tiny vertical slice and routing experiments.
It has the same bounded local-top-K contract intended for a Tantivy adapter.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from .identity import UrlId
from .snippets import make_snippet

_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)
_FIELD_WEIGHTS: Mapping[str, float] = {
    "title": 3.0,
    "headings": 2.0,
    "url": 1.5,
    "anchor": 1.25,
    "host": 1.2,
    "domain": 1.1,
    "body": 1.0,
}


@dataclass(frozen=True, slots=True)
class SearchDocument:
    url_id: UrlId
    url: str
    title: str
    body: str
    headings: str = ""
    anchor: str = ""
    host: str = ""
    domain: str = ""


@dataclass(frozen=True, slots=True)
class SearchHit:
    url_id: UrlId
    score: float
    title: str
    url: str
    snippet: str


class SearchIndex(Protocol):
    def search(self, query: str, top_k: int) -> tuple[SearchHit, ...]: ...


class Bm25fIndex:
    """Compact postings-only lexical segment with deterministic local ranking."""

    def __init__(self, documents: Iterable[SearchDocument]) -> None:
        self.documents = tuple(documents)
        self._by_id = {document.url_id.hex: document for document in self.documents}
        if len(self._by_id) != len(self.documents):
            raise ValueError("documents must have unique UrlIds within an index segment")
        self._postings: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        self._field_lengths: dict[str, dict[str, int]] = {}
        for document in self.documents:
            document_id = document.url_id.hex
            lengths: dict[str, int] = {}
            for field, weight in _FIELD_WEIGHTS.items():
                if weight == 0:
                    continue
                terms = _terms(_document_field(document, field))
                lengths[field] = len(terms)
                for term, frequency in Counter(terms).items():
                    self._postings[term].setdefault(document_id, {})[field] = frequency
            self._field_lengths[document_id] = lengths
        self._average_lengths = {
            field: sum(lengths.get(field, 0) for lengths in self._field_lengths.values()) / max(1, len(self.documents))
            for field in _FIELD_WEIGHTS
        }

    def search(self, query: str, top_k: int) -> tuple[SearchHit, ...]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_terms = tuple(dict.fromkeys(_terms(query)))
        candidate_ids = set().union(*(self._postings.get(term, {}) for term in query_terms)) if query_terms else set()
        scores: dict[str, float] = {candidate_id: 0.0 for candidate_id in candidate_ids}
        document_count = len(self.documents)
        for term in query_terms:
            postings = self._postings.get(term, {})
            if not postings:
                continue
            inverse_document_frequency = math.log(1 + (document_count - len(postings) + 0.5) / (len(postings) + 0.5))
            for document_id, fields in postings.items():
                scores[document_id] += inverse_document_frequency * _bm25f_score(
                    fields, self._field_lengths[document_id], self._average_lengths
                )
        hits = (
            SearchHit(document.url_id, score, document.title, document.url, make_snippet(document.body, query))
            for document_id, score in scores.items()
            for document in (self._by_id[document_id],)
        )
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.url_id.hex))[:top_k])

    def to_payload(self) -> bytes:
        """Safe JSON payload suitable for a project-owned immutable shard."""
        records = [{**asdict(document), "url_id": document.url_id.hex} for document in self.documents]
        return json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes) -> Bm25fIndex:
        try:
            records = json.loads(payload)
            documents = tuple(
                SearchDocument(
                    url_id=UrlId(bytes.fromhex(record.pop("url_id"))),
                    **record,
                )
                for record in records
            )
        except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
            raise ValueError("invalid lexical index payload") from exc
        return cls(documents)


def _terms(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN.finditer(value))


def _document_field(document: SearchDocument, field: str) -> str:
    return document.url if field == "url" else str(getattr(document, field))


def _bm25f_score(
    fields: Mapping[str, int], lengths: Mapping[str, int], averages: Mapping[str, float], *, k1: float = 1.2, b: float = 0.75
) -> float:
    weighted_frequency = sum(
        _FIELD_WEIGHTS[field] * frequency / (1 - b + b * lengths[field] / max(1.0, averages[field]))
        for field, frequency in fields.items()
    )
    return weighted_frequency * (k1 + 1) / (k1 + weighted_frequency)
