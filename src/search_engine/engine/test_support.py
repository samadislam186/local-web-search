"""Reusable deterministic fixtures for unit tests; not used by production serving."""

from __future__ import annotations

from .canonicalization import canonicalize_url
from .identity import UrlId
from .index import Bm25fIndex, SearchDocument
from .routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata
from .service import SearchService


def service_for_tests() -> SearchService:
    canonical = canonicalize_url("https://example.com/python")
    document = SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python crawler", "crawl the web")
    index = Bm25fIndex((document,))
    metadata = RoutingMetadata("g1", (ShardRoutingMetadata.from_terms("s1", "g1", 1, 100, ("python", "crawler")),))
    return SearchService(QueryPlanner(metadata), {"s1": index})
