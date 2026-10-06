"""Lazy, cache-bounded access to immutable lexical index shards."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .cache import BoundedShardCache
from .index import Bm25fIndex, SearchIndex
from .manifest import ManifestStore
from .storage import read_shard


class IndexProvider(Protocol):
    def get(self, shard_id: str) -> SearchIndex | None: ...

    def stats(self) -> dict[str, int]: ...


class InMemoryIndexProvider:
    def __init__(self, indexes: Mapping[str, SearchIndex]) -> None:
        self.indexes = dict(indexes)

    def get(self, shard_id: str) -> SearchIndex | None:
        return self.indexes.get(shard_id)

    def stats(self) -> dict[str, int]:
        return {"loaded_shards": len(self.indexes), "active_shard_bytes": 0}


class ManifestIndexProvider:
    """Loads a lexical shard only after planner selection and cache admission."""

    def __init__(self, store: ManifestStore, generation: str, *, max_active_shards: int, max_active_bytes: int) -> None:
        manifest = store.validate_generation(generation)
        self._paths: dict[str, tuple[Path, int]] = {}
        for artifact in manifest.artifacts:
            if artifact.header.shard_type == "lexical_index":
                path = store.generations / generation / artifact.relative_path
                self._paths[artifact.header.shard_id] = (path, artifact.header.payload_length)
        self._cache: BoundedShardCache[str, Bm25fIndex] = BoundedShardCache(
            max_entries=max_active_shards, max_bytes=max_active_bytes
        )

    def get(self, shard_id: str) -> SearchIndex | None:
        entry = self._paths.get(shard_id)
        if entry is None:
            return None
        path, payload_size = entry
        return self._cache.get_or_load(shard_id, payload_size, lambda: Bm25fIndex.from_payload(read_shard(path)[1]))

    def stats(self) -> dict[str, int]:
        snapshot = self._cache.snapshot()
        return {"loaded_shards": snapshot.active_entries, "active_shard_bytes": snapshot.active_bytes}
