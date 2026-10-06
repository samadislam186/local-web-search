import pytest
from fastapi.testclient import TestClient
from search_engine.canonicalization import canonicalize_url
from search_engine.feedback import FeedbackKind, LocalFeedbackAggregator
from search_engine.identity import UrlId
from search_engine.index import Bm25fIndex, SearchDocument
from search_engine.ranking import RankingSignals
from search_engine.routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata
from search_engine.service import SearchService

from search_engine.api import create_app


def _document(url: str, title: str, body: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, title, body)


def _service() -> SearchService:
    document = _document("https://example.com/python", "Python crawler", "crawl the web")
    second = _document("https://example.com/python-two", "Python index", "index the web")
    index = Bm25fIndex((document, second))
    metadata = RoutingMetadata("g1", (ShardRoutingMetadata.from_terms("s1", "g1", 1, 100, ("python", "crawler")),))
    return SearchService(
        QueryPlanner(metadata),
        {"s1": index},
        signals={document.url_id.hex: RankingSignals(authority=1.0), second.url_id.hex: RankingSignals(authority=1.0)},
    )


def test_search_service_plans_merges_and_ranks() -> None:
    response = _service().search("python", explain=True)
    assert response.results[0].hit.title == "Python crawler"
    assert response.plan.candidate_shards[0].shard_id == "s1"


def test_search_service_bounds_per_shard_top_k_and_merges_duplicate_ids() -> None:
    document = _document("https://example.com/python", "Python", "crawler")

    class RecordingIndex:
        def __init__(self, score: float) -> None:
            self.score = score
            self.requested: list[int] = []

        def search(self, _query: str, top_k: int):
            self.requested.append(top_k)
            return (SearchHit(document.url_id, self.score, document.title, document.url, "crawler"),)

    from search_engine.index import SearchHit

    first, second = RecordingIndex(1.0), RecordingIndex(2.0)
    metadata = RoutingMetadata(
        "g1",
        (
            ShardRoutingMetadata.from_terms("a", "g1", 1, 100, ("python",)),
            ShardRoutingMetadata.from_terms("b", "g1", 1, 100, ("python",)),
        ),
    )
    service = SearchService(QueryPlanner(metadata), {"a": first, "b": second}, per_shard_top_k=25)
    response = service.search("python", top_k=100, offset=50)
    assert first.requested == [25] and second.requested == [25]
    assert len(response.results) == 0


def test_search_service_applies_opt_in_runtime_feedback_to_explain_output() -> None:
    service = _service()
    feedback = LocalFeedbackAggregator()
    service = SearchService(
        service.planner,
        service.indexes,
        signals=service.signals,
        feedback=feedback,
        feedback_salt=b"test-local-feedback-salt",
    )
    first_id = service.search("python").results[0].hit.url_id.hex
    for _ in range(10):
        service.record_feedback("python", first_id, FeedbackKind.IMPRESSION)
    service.record_feedback("python", first_id, FeedbackKind.SELECT)
    response = service.search("python", explain=True)
    assert response.results[0].features["feedback"] == pytest.approx(0.01)


def test_api_exposes_search_and_explain_mode() -> None:
    client = TestClient(create_app(_service(), generation="g1"))
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post("/search", json={"query": "python", "explain": True})
    assert response.status_code == 200
    assert response.json()["results"][0]["features"]["authority"] == 1.0
    assert response.json()["results"][0]["snippet"] == "crawl the web"
    paged = client.post("/search", json={"query": "python", "top_k": 1, "offset": 1})
    assert paged.json()["results"][0]["rank"] == 2
    assert client.get("/stats").json()["loaded_shards"] == 1
    assert client.post("/search", json={"query": "python", "domain_filter": "other.example"}).json()["results"] == []
    assert client.post(
        "/feedback", json={"query": "python", "url_id": "a" * 32, "kind": "select"}
    ).status_code == 503


def test_api_accepts_feedback_only_for_opt_in_services() -> None:
    service = _service()
    service = SearchService(
        service.planner,
        service.indexes,
        signals=service.signals,
        feedback=LocalFeedbackAggregator(),
        feedback_salt=b"test-local-feedback-salt",
    )
    client = TestClient(create_app(service, generation="g1"))
    url_id = service.search("python").results[0].hit.url_id.hex
    response = client.post("/feedback", json={"query": "python", "url_id": url_id, "kind": "select"})
    assert response.json() == {"accepted": True}
    assert client.post("/feedback", json={"query": "python", "url_id": "A" * 32, "kind": "select"}).status_code == 422
