import json

import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.graph import HyperlinkGraph, derive_host_domain_graphs
from search_engine.identity import UrlId
from search_engine.pagerank import compute_pagerank


def _id(url: str) -> UrlId:
    return UrlId.from_canonical_url(canonicalize_url(url))


def test_graph_round_trip_uses_compact_ordinals() -> None:
    first, second = _id("https://a.example/"), _id("https://b.example/")
    graph = HyperlinkGraph.from_url_edges([(first, second), (first, second)])
    restored = HyperlinkGraph.from_payload(graph.to_payload())
    assert len(restored.edges) == 1
    assert restored.outgoing(restored.ordinals.ordinal_of(first)) == (restored.ordinals.ordinal_of(second),)


def test_pagerank_rewards_link_target_and_handles_dangling_nodes() -> None:
    first, second, third = _id("https://a.example/"), _id("https://b.example/"), _id("https://c.example/")
    graph = HyperlinkGraph.from_url_edges([(first, second), (third, second)])
    result = compute_pagerank(graph)
    scores = result.by_url_id(graph)
    assert result.converged
    assert scores[second.hex] > scores[first.hex]
    assert sum(result.scores) == pytest.approx(1.0)


def test_pagerank_reports_iteration_progress_without_changing_scores() -> None:
    first, second = _id("https://a.example/"), _id("https://b.example/")
    graph = HyperlinkGraph.from_url_edges([(first, second)])
    updates: list[tuple[int, float]] = []
    result = compute_pagerank(graph, tolerance=1e-6, progress=lambda iteration, delta: updates.append((iteration, delta)))
    assert updates
    assert updates[-1][0] == result.iterations
    assert all(delta >= 0 for _iteration, delta in updates)


def test_graph_rejects_non_compact_edge_payloads() -> None:
    first, second = _id("https://a.example/"), _id("https://b.example/")
    graph = HyperlinkGraph.from_url_edges([(first, second)])
    payload_dict = json.loads(graph.to_payload())
    payload_dict["edges"].append([0, 1])
    with pytest.raises(ValueError, match="edges"):
        HyperlinkGraph.from_payload(json.dumps(payload_dict).encode("utf-8"))


def test_graph_payload_streaming_is_byte_equivalent_and_bounded() -> None:
    graph = HyperlinkGraph.from_url_edges([(UrlId(b"a" * 16), UrlId(b"b" * 16))])
    chunks = tuple(graph.iter_payload_chunks(chunk_size=7))
    assert b"".join(chunks) == graph.to_payload()
    assert all(0 < len(chunk) <= 7 for chunk in chunks)


def test_graph_build_consumes_url_edges_in_batches() -> None:
    first, second, third = _id("https://a.example/"), _id("https://b.example/"), _id("https://c.example/")
    graph = HyperlinkGraph.from_url_edge_batches(iter((((first, second),), ((third, second),))))
    assert graph.outgoing(graph.ordinals.ordinal_of(first)) == (graph.ordinals.ordinal_of(second),)
    assert graph.outgoing(graph.ordinals.ordinal_of(third)) == (graph.ordinals.ordinal_of(second),)


def test_host_and_domain_graphs_are_logical_and_retain_edge_counts() -> None:
    first, second, third = _id("https://docs.example.com/a"), _id("https://www.example.com/b"), _id("https://other.test/c")
    graph = HyperlinkGraph.from_url_edges([(first, second), (first, third), (second, third)])
    layers = derive_host_domain_graphs(
        graph,
        {first: "https://docs.example.com/a", second: "https://www.example.com/b", third: "https://other.test/c"},
        domain_for_host=lambda host: ".".join(host.rsplit(".", 2)[-2:]),
    )
    assert layers.hosts.nodes == ("docs.example.com", "other.test", "www.example.com")
    assert tuple((edge.source, edge.target, edge.count) for edge in layers.domains.edges) == (
        ("example.com", "example.com", 1),
        ("example.com", "other.test", 2),
    )
