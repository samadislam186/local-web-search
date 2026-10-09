"""LLM-independent agent-tool adapter around the local search service."""

from __future__ import annotations

from ..engine.service import SearchService


def search_web(
    service: SearchService,
    query: str,
    *,
    top_k: int = 10,
    freshness: str | None = None,
    domain_filter: str | None = None,
    language: str | None = None,
) -> list[dict[str, object]]:
    """Return portable result evidence without coupling the adapter to an LLM runtime."""
    response = service.search(query, top_k=top_k, language=language, domain_filter=domain_filter, freshness=freshness)
    return [
        {
            "url": result.hit.url,
            "title": result.hit.title,
            "snippet": result.hit.snippet,
            "score": result.score,
            "domain": result.domain,
        }
        for result in response.results
    ]
