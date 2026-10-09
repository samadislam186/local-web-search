"""Lazy, cache-bounded access to immutable lexical index shards."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from time import perf_counter
from typing import Protocol

from .cache import BoundedShardCache
from .index import Bm25fIndex, SearchIndex
from .manifest import ManifestStore
from .storage import ArtifactValidationError, ShardHeader, read_shard
from .tantivy_adapter import TantivyIndex


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
        self._paths: dict[str, tuple[Path, int, str, ShardHeader]] = {}
        for artifact in manifest.artifacts:
            if artifact.header.shard_type in {"lexical_index", "tantivy_index"}:
                path = store.generations / generation / artifact.relative_path
                if artifact.header.shard_id in self._paths:
                    raise ArtifactValidationError("duplicate lexical shard ID")
                self._paths[artifact.header.shard_id] = (path, artifact.header.payload_length, artifact.header.shard_type, artifact.header)
        self._cache: BoundedShardCache[str, SearchIndex] = BoundedShardCache(
            max_entries=max_active_shards, max_bytes=max_active_bytes
        )
        self._read_bytes = 0
        self._load_ms = 0.0

    def get(self, shard_id: str) -> SearchIndex | None:
        entry = self._paths.get(shard_id)
        if entry is None:
            return None
        path, payload_size, shard_type, expected_header = entry
        decoder = TantivyIndex.from_payload if shard_type == "tantivy_index" else Bm25fIndex.from_payload

        def load() -> SearchIndex:
            started = perf_counter()
            header, payload = read_shard(path)
            if header != expected_header:
                raise ArtifactValidationError("lexical shard changed after generation validation")
            result = decoder(payload)
            self._read_bytes += path.stat().st_size
            self._load_ms += (perf_counter() - started) * 1000
            return result

        return self._cache.get_or_load(shard_id, payload_size, load)

    def stats(self) -> dict[str, int]:
        snapshot = self._cache.snapshot()
        return {
            "loaded_shards": snapshot.active_entries,
            "active_shard_bytes": snapshot.active_bytes,
            "cache_hits": snapshot.hits,
            "cache_misses": snapshot.misses,
            "cache_rejections": snapshot.rejections,
            "shard_read_bytes": self._read_bytes,
            "shard_load_ms": int(self._load_ms),
        }
