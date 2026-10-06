"""Minimal read-only FastAPI surface for a configured SearchService."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..engine.feedback import FeedbackKind
from ..engine.service import SearchService


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10_000)
    language: str | None = Field(default=None, max_length=32)
    domain_filter: str | None = Field(default=None, max_length=253)
    explain: bool = False


class FeedbackRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    url_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: FeedbackKind


def create_app(service: SearchService, *, generation: str) -> FastAPI:
    app = FastAPI(title="OpenSearch Local", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"generation": generation, "service_version": "0.1.0"}

    @app.get("/capabilities")
    def capabilities() -> dict[str, object]:
        return {"lexical_retrieval": True, "semantic_retrieval": False, "explain": True}

    @app.get("/stats")
    def stats() -> dict[str, int]:
        return service.stats()

    @app.post("/search")
    def search(request: SearchRequest) -> dict[str, object]:
        try:
            response = service.search(
                request.query,
                top_k=request.top_k,
                offset=request.offset,
                language=request.language,
                domain_filter=request.domain_filter,
                explain=request.explain,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "query": response.query,
            "results": [
                {
                    "rank": rank,
                    "url": result.hit.url,
                    "title": result.hit.title,
                    "snippet": result.hit.snippet,
                    "score": result.score,
                    "domain": result.domain,
                    **({"features": result.features} if request.explain else {}),
                }
                for rank, result in enumerate(response.results, start=request.offset + 1)
            ],
            "routing": {
                "shards_opened": len(response.plan.candidate_shards),
                "max_shards": response.plan.max_shards,
                "byte_budget": response.plan.byte_budget,
            },
            "timing_ms": {"total": response.total_ms},
        }

    @app.post("/feedback")
    def feedback(request: FeedbackRequest) -> dict[str, bool]:
        try:
            service.record_feedback(request.query, request.url_id, request.kind)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"accepted": True}

    return app
