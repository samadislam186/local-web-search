from pathlib import Path

import pytest
from search_engine.routing import RoutingMetadata, ShardRoutingMetadata
from search_engine.routing_store import read_routing_metadata, write_routing_metadata
from search_engine.storage import ArtifactValidationError


def test_flat_routing_metadata_is_checksumed_and_mmap_readable(tmp_path: Path) -> None:
    metadata = RoutingMetadata("g1", (ShardRoutingMetadata.from_terms("s1", "g1", 1, 10, ("python",)),))
    path = tmp_path / "routing.meta"
    checksum = write_routing_metadata(path, metadata)
    assert read_routing_metadata(path, expected_checksum=checksum) == metadata
    with pytest.raises(ArtifactValidationError, match="checksum"):
        read_routing_metadata(path, expected_checksum="0" * 64)
