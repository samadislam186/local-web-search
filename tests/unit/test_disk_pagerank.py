import sqlite3

import pytest
from search_engine.graph import HyperlinkGraph
from search_engine.identity import UrlId
from search_engine.pagerank import compute_pagerank

from search_engine.engine.disk_pagerank import DiskPageRank


@pytest.mark.parametrize("edges", ((), ((0, 1),), ((0, 1), (1, 0)), ((0, 0), (0, 1), (1, 2))))
def test_disk_pagerank_matches_reference(tmp_path, edges) -> None:
    from contextlib import closing

    nodes = [UrlId(number.to_bytes(16, "big")) for number in range(4)]
    url_edges = [(nodes[source], nodes[target]) for source, target in edges]
    graph = HyperlinkGraph.from_url_edges(url_edges, nodes=nodes)
    expected = compute_pagerank(graph).by_url_id(graph)
    with closing(sqlite3.connect(tmp_path / "rank.sqlite3")) as connection:
        connection.execute("PRAGMA cache_size=-1024")
        connection.execute("PRAGMA temp_store=FILE")
        rank = DiskPageRank(connection)
        rank.add_nodes(iter(nodes))
        rank.add_edges(iter(url_edges + url_edges))  # duplicate links carry no extra weight
        rank.add_edges(((nodes[0], UrlId(b"x" * 16)),))  # ignore destinations outside this generation
        result = rank.compute()
        batches = list(rank.score_batches(batch_size=2))
        actual = {identifier: score for batch in batches for identifier, score in batch.items()}
        assert actual == pytest.approx(expected, abs=1e-10)
        assert sum(actual.values()) == pytest.approx(1.0)
        assert all(len(batch) <= 2 for batch in batches)
        assert result.nodes == 4
        assert result.converged


def test_empty_disk_pagerank_and_nonconvergence(tmp_path) -> None:
    from contextlib import closing

    with closing(sqlite3.connect(tmp_path / "rank.sqlite3")) as connection:
        rank = DiskPageRank(connection)
        assert rank.compute().iterations == 0
        nodes = [UrlId(number.to_bytes(16, "big")) for number in range(2)]
        rank.add_nodes(nodes)
        rank.add_edges(((nodes[0], nodes[1]),))
        assert not rank.compute(max_iterations=1).converged
        with pytest.raises(ValueError, match="parameters"):
            rank.compute(damping=1)
