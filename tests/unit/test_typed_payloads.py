from pathlib import Path

import pytest
from search_engine.graph import HyperlinkGraph
from search_engine.identity import UrlId
from search_engine.storage import ArtifactValidationError
from search_engine.typed_payloads import (
    DocumentRecord,
    RankingRecord,
    UrlRecord,
    read_document_shard,
    read_graph_shard,
    read_metadata_shard,
    read_ranking_shard,
    read_url_shard,
    write_document_shard,
    write_graph_shard,
    write_metadata_shard,
    write_ranking_shard,
    write_url_shard,
)


def _url_id(value: int) -> UrlId:
    return UrlId(value.to_bytes(16, "big"))


def test_typed_records_round_trip_with_stable_url_ids_and_version_references(tmp_path: Path) -> None:
    url = UrlRecord(_url_id(1), "https://example.com/")
    document = DocumentRecord(_url_id(1), url.canonical_url, "Title", "Body", "a" * 64, "en")
    ranking = RankingRecord(_url_id(1), 0.75)

    url_header = write_url_shard(tmp_path / "urls.shard", (url,), shard_id="urls", generation="g1")
    write_document_shard(tmp_path / "documents.shard", (document,), shard_id="documents", generation="g1")
    write_ranking_shard(tmp_path / "ranking.shard", (ranking,), shard_id="ranking", generation="g1")
    write_metadata_shard(tmp_path / "metadata.shard", {"source_crawl": "CC-1", "documents": 1}, shard_id="meta", generation="g1")

    assert url_header.referenced_versions == {"canonicalization": 1, "url_id": 1}
    assert read_url_shard(tmp_path / "urls.shard") == (url,)
    assert read_document_shard(tmp_path / "documents.shard") == (document,)
    assert read_ranking_shard(tmp_path / "ranking.shard") == (ranking,)
    assert read_metadata_shard(tmp_path / "metadata.shard") == {"documents": 1, "source_crawl": "CC-1"}


def test_graph_payload_round_trips_generation_local_ordinals(tmp_path: Path) -> None:
    graph = HyperlinkGraph.from_url_edges([(_url_id(1), _url_id(2))])
    write_graph_shard(tmp_path / "graph.shard", graph, shard_id="links", generation="g1")
    assert read_graph_shard(tmp_path / "graph.shard") == graph


def test_typed_payloads_reject_duplicate_stable_ids(tmp_path: Path) -> None:
    record = UrlRecord(_url_id(1), "https://example.com/")
    write_url_shard(tmp_path / "urls.shard", (record, record), shard_id="urls", generation="g1")
    with pytest.raises(ArtifactValidationError, match="duplicate UrlId"):
        read_url_shard(tmp_path / "urls.shard")
