"""Tiny-profile generation builder and read-only serving loader."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from time import perf_counter

from .authority import authority_from_payload, authority_to_payload
from .index import Bm25fIndex, SearchDocument
from .index_provider import ManifestIndexProvider
from .manifest import Manifest, ManifestStore, safe_generation
from .observability import log_event
from .ranking import RankingSignals
from .routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata
from .security import PinnedKeyVerifier, ReleaseSigner
from .service import SearchService
from .storage import (
    MAX_SHARD_PAYLOAD_BYTES,
    ShardArtifact,
    read_shard,
    read_shard_header,
    safe_shard_relative_path,
    write_shard,
    write_shard_stream,
)
from .tantivy_adapter import TantivyIndex, require_tantivy, tantivy_capability

_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)


@dataclass(frozen=True, slots=True)
class RetainedSegment:
    source_directory: Path
    index: ShardArtifact
    catalog: ShardArtifact


def load_segment_catalog(segment: RetainedSegment) -> tuple[ShardRoutingMetadata, tuple[str, ...], str | None]:
    """Read a bounded, checksum-verified inventory paired with one lexical shard."""
    header, payload = read_shard(segment.source_directory / safe_shard_relative_path(segment.catalog.relative_path))
    if header != segment.catalog.header:
        raise ValueError("catalog changed after validation")
    try:
        raw = json.loads(payload)
        if raw["format_version"] != 1 or raw["index_checksum"] != segment.index.header.checksum:
            raise ValueError("catalog/index mismatch")
        metadata = RoutingMetadata.from_bytes(json.dumps({
            "generation": segment.index.header.generation, "format_version": 2, "shards": [raw["routing"]],
        }).encode()).shards[0]
        identifiers = tuple(raw["url_ids"])
        if metadata.shard_id != segment.index.header.shard_id or metadata.document_count != len(identifiers):
            raise ValueError("catalog identity/count mismatch")
        if len(set(identifiers)) != len(identifiers) or any(
            not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier) for identifier in identifiers
        ):
            raise ValueError("invalid catalog URL IDs")
        version = raw.get("backend_version")
        if version is not None and not isinstance(version, str):
            raise TypeError("invalid catalog backend version")
        return metadata, identifiers, version
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid segment catalog") from exc


def publish_lexical_generation(
    root: Path,
    generation: str,
    segments: Mapping[str, tuple[SearchDocument, ...]] | Iterable[tuple[str, tuple[SearchDocument, ...]]],
    *,
    authority_scores: dict[str, float] | Iterable[dict[str, float]] | None = None,
    target_shard_payload_bytes: int | None = None,
    backend: str = "reference",
    signer: ReleaseSigner | None = None,
    retained_segments: Iterable[RetainedSegment] = (),
) -> Manifest:
    """Build immutable reference lexical and routing artifacts, then activate them.

    Each input segment becomes one independently versioned index shard. Production
    compaction/partition assignment remains a separate measured concern.
    """
    if backend not in {"reference", "tantivy"}:
        raise ValueError("backend must be reference or tantivy")
    if backend == "tantivy":
        require_tantivy()
    started = perf_counter()
    if target_shard_payload_bytes is not None and not 0 < target_shard_payload_bytes <= MAX_SHARD_PAYLOAD_BYTES:
        raise ValueError("target_shard_payload_bytes must be within the supported shard payload limit")
    generation = safe_generation(generation)
    store = ManifestStore(root, require_signatures=signer is not None, verifier=signer.verifier() if signer else None)
    generation_dir = store.generations / generation
    if (generation_dir / "manifest.json").exists():
        raise FileExistsError(f"generation already exists: {generation}")
    artifacts: list[ShardArtifact] = []
    items = sorted(segments.items()) if isinstance(segments, Mapping) else segments
    routing_relative_path = "metadata/routing.shard"
    routing_header = write_shard_stream(
        generation_dir / routing_relative_path,
        _publish_segments(generation_dir, generation, items, artifacts, target_shard_payload_bytes, backend, retained_segments),
        shard_type="routing_metadata",
        shard_id="routing",
        generation=generation,
    )
    artifacts.append(ShardArtifact(routing_relative_path, routing_header))
    if authority_scores is not None:
        batches = (authority_scores,) if isinstance(authority_scores, dict) else authority_scores
        for number, scores in enumerate(batches):
            authority_id = "authority" if number == 0 else f"authority-{number:06d}"
            authority_relative_path = f"ranking/{authority_id}.shard"
            authority_header = write_shard(
                generation_dir / authority_relative_path,
                authority_to_payload(scores),
                shard_type="ranking_authority",
                shard_id=authority_id,
                generation=generation,
            )
            artifacts.append(ShardArtifact(authority_relative_path, authority_header))
    manifest = Manifest.create(generation, tuple(artifacts))
    manifest_path = store.write_generation(manifest)
    if signer is not None:
        store.write_generation(manifest, signer.sign(manifest_path.read_bytes()))
    activated = store.activate(generation)
    log_event(logging.getLogger(__name__), "generation_activated", generation=generation, artifacts=len(artifacts),
              backend=backend, duration_ms=(perf_counter() - started) * 1000,
              payload_bytes=sum(artifact.header.payload_length for artifact in artifacts))
    return activated


def _publish_segments(generation_dir, generation, items, artifacts, target_bytes, backend, retained_segments=()):
    """Write indexes while streaming each routing summary directly to its shard."""
    from .routing import ROUTING_METADATA_VERSION

    yield (f'{{"format_version":{ROUTING_METADATA_VERSION},"generation":' + json.dumps(generation) + ',"shards":[').encode()
    seen: set[str] = set()
    published: set[str] = set()
    for retained in retained_segments:
        shard, _identifiers, version = load_segment_catalog(retained)
        if retained.index.header.shard_type == "tantivy_index" and version != tantivy_capability().version:
            raise ValueError("cannot retain an incompatible native index")
        if shard.shard_id in published:
            raise ValueError("duplicate retained segment ID")
        for artifact in (retained.index, retained.catalog):
            source = retained.source_directory / safe_shard_relative_path(artifact.relative_path)
            destination = generation_dir / safe_shard_relative_path(artifact.relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if read_shard_header(source) != artifact.header:
                raise ValueError("retained artifact changed after validation")
            try:
                os.link(source, destination)
            except FileExistsError:
                raise
            except OSError:
                # Portable fallback; immutable generations remain self-contained.
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 64 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
            artifacts.append(replace(artifact, source_generation=artifact.header.generation))
        if published:
            yield b","
        published.add(shard.shard_id)
        record = asdict(replace(shard, generation=generation))
        record["bloom"]["bits"] = shard.bloom.bits.hex()
        yield json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    for shard_id, documents in items:
        safe_generation(shard_id)
        if shard_id in seen:
            raise ValueError("duplicate segment ID")
        seen.add(shard_id)
        for published_id, published_documents, payload in _bounded_segments(shard_id, documents, target_bytes, backend, generation_dir):
            if published_id in published:
                raise ValueError("duplicate published segment ID")
            if published:
                yield b","
            published.add(published_id)
            relative_path = f"indexes/{published_id}.shard"
            header = write_shard(generation_dir / relative_path, payload,
                                 shard_type="tantivy_index" if backend == "tantivy" else "lexical_index",
                                 shard_id=published_id, generation=generation)
            artifacts.append(ShardArtifact(relative_path, header))
            shard = ShardRoutingMetadata.from_documents(published_id, generation,
                                                        (generation_dir / relative_path).stat().st_size, published_documents)
            record = asdict(shard)
            record["bloom"]["bits"] = shard.bloom.bits.hex()
            catalog_path = f"catalogs/{published_id}.shard"
            catalog_header = write_shard(
                generation_dir / catalog_path,
                json.dumps({"format_version": 1, "index_checksum": header.checksum, "routing": record,
                            "backend_version": tantivy_capability().version if backend == "tantivy" else None,
                            "url_ids": [document.url_id.hex for document in published_documents]},
                           sort_keys=True, separators=(",", ":")).encode(),
                shard_type="index_catalog", shard_id=published_id, generation=generation,
            )
            artifacts.append(ShardArtifact(catalog_path, catalog_header))
            yield json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    if not published:
        raise ValueError("at least one lexical segment is required")
    yield b"]}"


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
    shard_id: str, documents: tuple[SearchDocument, ...], target_payload_bytes: int | None, backend: str = "reference",
    temporary_root: Path | None = None,
) -> tuple[tuple[str, tuple[SearchDocument, ...], bytes], ...]:
    """Split deterministically until each serialized index is within its configured target."""
    payload = TantivyIndex.build_payload(documents, temporary_root=temporary_root) if backend == "tantivy" else Bm25fIndex(documents).to_payload()
    if target_payload_bytes is None or len(payload) <= target_payload_bytes:
        return ((shard_id, documents, payload),)
    if len(documents) == 1:
        raise ValueError(f"document in {shard_id!r} exceeds target_shard_payload_bytes")
    midpoint = len(documents) // 2
    return _bounded_segments(f"{shard_id}-0", documents[:midpoint], target_payload_bytes, backend, temporary_root) + _bounded_segments(
        f"{shard_id}-1", documents[midpoint:], target_payload_bytes, backend, temporary_root
    )
