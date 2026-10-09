"""Minimal read-only FastAPI surface for a configured SearchService."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..engine.feedback import FeedbackKind
from ..engine.observability import correlation_context
from ..engine.publisher import load_active_search_service
from ..engine.security import PinnedKeyVerifier
from ..engine.service import SearchService
from ..engine.storage import ArtifactValidationError


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10_000)
    language: str | None = Field(default=None, max_length=32)
    domain_filter: str | None = Field(default=None, max_length=253)
    explain: bool = False
    freshness: Literal["day", "week", "month", "year"] | None = None


class FeedbackRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    url_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: FeedbackKind


def create_app(service: SearchService, *, generation: str) -> FastAPI:
    app = FastAPI(title="OpenSearch Local", version="0.1.0")

    @app.middleware("http")
    async def request_context(request, call_next):
        with correlation_context() as correlation_id:
            response = await call_next(request)
            response.headers["X-Request-ID"] = correlation_id
            return response

    @app.get("/metrics")
    def metrics() -> dict[str, object]:
        return {"generation": generation, **asdict(service.metrics.snapshot()), "resources": service.stats()}

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
                freshness=request.freshness,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ArtifactValidationError as exc:
            service.metrics.increment("query_artifact_errors")
            raise HTTPException(status_code=503, detail="active generation artifact is unavailable or invalid") from exc
        return {
            "query": response.query,
            "pagination": {"top_k": request.top_k, "offset": request.offset, "returned": len(response.results)},
            "filters": {
                "language": request.language,
                "domain": request.domain_filter
                or (response.plan.analysis.domain_filter if response.plan.analysis else None),
                "freshness": request.freshness or (response.plan.analysis.freshness if response.plan.analysis else None),
            },
            "results": [
                {
                    "rank": rank,
                    "url": result.hit.url,
                    "title": result.hit.title,
                    "snippet": result.hit.snippet,
                    "score": result.score,
                    "domain": result.domain,
                    **(
                        {"features": result.features, "contributions": result.contributions}
                        if request.explain
                        else {}
                    ),
                }
                for rank, result in enumerate(response.results, start=request.offset + 1)
            ],
            "routing": {
                "shards_opened": len(response.plan.candidate_shards),
                "max_shards": response.plan.max_shards,
                "byte_budget": response.plan.byte_budget,
                "estimated_latency_ms": response.plan.estimated_latency_ms,
                "stopped_early": response.plan.stopped_early,
                "stop_reason": response.plan.stop_reason,
                "budget_exhausted": response.plan.budget_exhausted,
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


def create_generation_app(
    root: Path,
    *,
    require_signatures: bool = True,
    verifier: PinnedKeyVerifier | None = None,
    max_shards: int = 10,
    byte_budget: int = 1 << 30,
    max_active_shards: int = 10,
    max_active_bytes: int = 1 << 30,
) -> tuple[FastAPI, SearchService]:
    """Build a serving app solely from the active validated artifact generation."""
    service = load_active_search_service(
        root,
        require_signatures=require_signatures,
        verifier=verifier,
        max_shards=max_shards,
        byte_budget=byte_budget,
        max_active_shards=max_active_shards,
        max_active_bytes=max_active_bytes,
    )
    return create_app(service, generation=service.planner.metadata.generation), service
