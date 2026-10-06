"""Tiny-profile generation builder and read-only serving loader."""

from __future__ import annotations

import re
from pathlib import Path

from .authority import authority_from_payload, authority_to_payload
from .index import Bm25fIndex, SearchDocument
from .index_provider import ManifestIndexProvider
from .manifest import Manifest, ManifestStore, safe_generation
from .ranking import RankingSignals
from .routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata
from .security import PinnedKeyVerifier
from .service import SearchService
from .storage import MAX_SHARD_PAYLOAD_BYTES, ShardArtifact, read_shard, write_shard

_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)


def publish_lexical_generation(
    root: Path,
    generation: str,
    segments: dict[str, tuple[SearchDocument, ...]],
    *,
    authority_scores: dict[str, float] | None = None,
    target_shard_payload_bytes: int | None = None,
) -> Manifest:
    """Build immutable reference lexical and routing artifacts, then activate them.

    Each input segment becomes one independently versioned index shard. Production
    compaction/partition assignment remains a separate measured concern.
    """
    if not segments:
        raise ValueError("at least one lexical segment is required")
    if target_shard_payload_bytes is not None and not 0 < target_shard_payload_bytes <= MAX_SHARD_PAYLOAD_BYTES:
        raise ValueError("target_shard_payload_bytes must be within the supported shard payload limit")
    generation = safe_generation(generation)
    store = ManifestStore(root, require_signatures=False)
    generation_dir = store.generations / generation
    if (generation_dir / "manifest.json").exists():
        raise FileExistsError(f"generation already exists: {generation}")
    artifacts: list[ShardArtifact] = []
    routing_shards: list[ShardRoutingMetadata] = []
    for shard_id, documents in sorted(segments.items()):
        for published_id, published_documents, payload in _bounded_segments(
            shard_id, documents, target_shard_payload_bytes
        ):
            relative_path = f"indexes/{published_id}.shard"
            header = write_shard(
                generation_dir / relative_path,
                payload,
                shard_type="lexical_index",
                shard_id=published_id,
                generation=generation,
            )
            artifacts.append(ShardArtifact(relative_path, header))
            routing_shards.append(
                ShardRoutingMetadata.from_terms(
                    published_id,
                    generation,
                    len(published_documents),
                    (generation_dir / relative_path).stat().st_size,
                    _document_terms(published_documents),
                )
            )
    routing_payload = RoutingMetadata(generation, tuple(routing_shards)).to_bytes()
    routing_relative_path = "metadata/routing.shard"
    routing_header = write_shard(
        generation_dir / routing_relative_path,
        routing_payload,
        shard_type="routing_metadata",
        shard_id="routing",
        generation=generation,
    )
    artifacts.append(ShardArtifact(routing_relative_path, routing_header))
    if authority_scores is not None:
        authority_relative_path = "ranking/authority.shard"
        authority_header = write_shard(
            generation_dir / authority_relative_path,
            authority_to_payload(authority_scores),
            shard_type="ranking_authority",
            shard_id="authority",
            generation=generation,
        )
        artifacts.append(ShardArtifact(authority_relative_path, authority_header))
    manifest = Manifest.create(generation, tuple(artifacts))
    store.write_generation(manifest)
    return store.activate(generation)


def load_active_search_service(
    root: Path,
    *,
    max_shards: int = 10,
    byte_budget: int = 1 << 30,
    minimum_routing_score: float = 0.0,
    require_signatures: bool = True,
    verifier: PinnedKeyVerifier | None = None,
    max_active_shards: int = 10,
    max_active_bytes: int = 1 << 30,
) -> SearchService:
    """Load only validated artifacts from the active immutable generation."""
    store = ManifestStore(root, require_signatures=require_signatures, verifier=verifier)
    manifest = store.active_manifest()
    routing: RoutingMetadata | None = None
    signals: dict[str, RankingSignals] = {}
    for artifact in manifest.artifacts:
        if artifact.header.shard_type == "routing_metadata":
            _header, payload = read_shard(store.generations / manifest.generation / artifact.relative_path)
            routing = RoutingMetadata.from_bytes(payload)
        elif artifact.header.shard_type == "ranking_authority":
            _header, payload = read_shard(store.generations / manifest.generation / artifact.relative_path)
            signals.update(authority_from_payload(payload))
    if routing is None:
        raise ValueError("active generation has no routing metadata")
    provider = ManifestIndexProvider(
        store, manifest.generation, max_active_shards=max_active_shards, max_active_bytes=max_active_bytes
    )
    planner = QueryPlanner(
        routing,
        max_shards=max_shards,
        byte_budget=byte_budget,
        minimum_score=minimum_routing_score,
    )
    return SearchService(planner, provider, signals=signals)


def _document_terms(documents: tuple[SearchDocument, ...]) -> tuple[str, ...]:
    values = " ".join(
        f"{document.url} {document.title} {document.body} {document.headings} {document.anchor}"
        for document in documents
    )
    return tuple(match.group(0).casefold() for match in _TOKEN.finditer(values))


def _bounded_segments(
    shard_id: str, documents: tuple[SearchDocument, ...], target_payload_bytes: int | None
) -> tuple[tuple[str, tuple[SearchDocument, ...], bytes], ...]:
    """Split deterministically until each serialized index is within its configured target."""
    payload = Bm25fIndex(documents).to_payload()
    if target_payload_bytes is None or len(payload) <= target_payload_bytes:
        return ((shard_id, documents, payload),)
    if len(documents) == 1:
        raise ValueError(f"document in {shard_id!r} exceeds target_shard_payload_bytes")
    midpoint = len(documents) // 2
    return _bounded_segments(f"{shard_id}-0", documents[:midpoint], target_payload_bytes) + _bounded_segments(
        f"{shard_id}-1", documents[midpoint:], target_payload_bytes
    )
