import pytest
from search_engine.agent import search_web
from search_engine.evaluation import (
    benchmark,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    routing_containment,
)
from search_engine.test_support import service_for_tests


def test_retrieval_and_routing_metrics() -> None:
    returned = ["a", "b", "c"]
    assert recall_at_k(returned, {"a", "x"}, 2) == 0.5
    assert precision_at_k(returned, {"a"}, 2) == 0.5
    assert reciprocal_rank(returned, {"b"}) == 0.5
    assert ndcg_at_k(returned, {"a": 3.0, "b": 1.0}, 2) == pytest.approx(1.0)
    assert routing_containment(["a", "b"], {"s1"}, {"a": "s1", "b": "s2"}) == 0.5


def test_benchmark_and_agent_adapter() -> None:
    result = benchmark(lambda: None, runs=2)
    assert result.runs == 2 and result.p95_ms >= 0
    results = search_web(service_for_tests(), "python")
    assert results[0]["title"] == "Python crawler"
    assert search_web(service_for_tests(), "python", domain_filter="other.example") == []
