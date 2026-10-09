import pytest
from search_engine.agent import search_web
from search_engine.evaluation import (
    JudgedQuery,
    benchmark,
    benchmark_pagerank,
    benchmark_search,
    evaluate_search,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    routing_containment,
)
from search_engine.graph import HyperlinkGraph
from search_engine.identity import UrlId
from search_engine.test_support import service_for_tests


def test_retrieval_and_routing_metrics() -> None:
    returned = ["a", "b", "c"]
    assert recall_at_k(returned, {"a", "x"}, 2) == 0.5
    assert precision_at_k(returned, {"a"}, 2) == 0.5
    assert reciprocal_rank(returned, {"b"}) == 0.5
    assert ndcg_at_k(returned, {"a": 3.0, "b": 1.0}, 2) == pytest.approx(1.0)
    assert routing_containment(["a", "b"], {"s1"}, {"a": "s1", "b": "s2"}) == 0.5


def test_benchmark_and_agent_adapter() -> None:
    disk_reads = iter((100, 125))
    result = benchmark(
        lambda: None,
        runs=2,
        rss_reader=lambda: 123,
        disk_read_reader=lambda: next(disk_reads),
        cache_snapshot=lambda: {"hits": 4, "misses": 1},
    )
    assert result.runs == 2 and result.p95_ms >= 0
    assert result.rss_bytes == 123
    assert result.disk_read_bytes == 25
    assert result.cpu_ms >= 0
    assert result.cache_metrics == {"hits": 4, "misses": 1}
    results = search_web(service_for_tests(), "python")
    assert results[0]["title"] == "Python crawler"
    assert search_web(service_for_tests(), "python", domain_filter="other.example") == []


def test_service_benchmark_captures_routing_and_loaded_shard_observations() -> None:
    result = benchmark_search(service_for_tests(), "python", runs=2)
    assert result.benchmark.runs == 2
    assert result.routing_shards == 1
    assert result.loaded_shards == 1


def test_pagerank_benchmark_uses_the_offline_graph_path() -> None:
    first, second = UrlId(bytes(16)), UrlId(bytes.fromhex("01" + "00" * 15))
    graph = HyperlinkGraph.from_url_edges(((first, second),), nodes=(first, second))
    assert benchmark_pagerank(graph, runs=1).runs == 1


def test_agent_adapter_enforces_freshness_and_rejects_invalid_windows() -> None:
    assert search_web(service_for_tests(), "python", freshness="week") == []
    with pytest.raises(ValueError, match="freshness"):
        search_web(service_for_tests(), "python", freshness="invalid")


def test_relevance_report_combines_real_search_and_routing_evidence() -> None:
    service = service_for_tests()
    url_id = service.search("python").results[0].hit.url_id.hex
    report = evaluate_search(
        service,
        (JudgedQuery("python", {url_id: 3}, (url_id,)),),
        top_k=1,
        document_shards={url_id: "s1"},
    )
    assert (report.precision, report.recall, report.mrr, report.ndcg, report.containment) == (1, 1, 1, 1, 1)
    assert '"report_version": 1' in report.to_json()
    with pytest.raises(ValueError, match="at least one"):
        evaluate_search(service, ())
