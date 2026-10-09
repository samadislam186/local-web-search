"""Bounded local index builder from durable crawl documents to a published generation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from blake3 import blake3

from .crawl_store import CrawledDocumentRepository
from .disk_pagerank import DiskPageRank
from .identity import UrlId
from .index import SearchDocument, documents_from_payload
from .manifest import Manifest, ManifestStore
from .publisher import RetainedSegment, load_segment_catalog, publish_lexical_generation
from .security import ReleaseSigner
from .state import CandidateRepository
from .storage import ShardArtifact, read_shard
from .tantivy_adapter import native_documents, tantivy_capability


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    manifest: Manifest
    document_count: int
    shard_count: int
    reused_shards: int = 0
    rebuilt_shards: int = 0


class LocalIndexBuilder:
    """Rebuild a complete immutable snapshot; source documents are marked after activation."""

    def __init__(self, documents: CrawledDocumentRepository, candidates: CandidateRepository) -> None:
        self.documents = documents
        self.candidates = candidates

    def build(
        self,
        root: Path,
        generation: str,
        *,
        max_documents: int,
        shard_count: int = 1,
        target_shard_payload_bytes: int | None = None,
        authority_scores: dict[str, float] | None = None,
        backend: str = "reference",
        signer: ReleaseSigner | None = None,
        segment_documents: int = 1_000,
        segment_source_bytes: int = 16 << 20,
        full_rebuild: bool = False,
    ) -> IndexBuildResult:
        if shard_count <= 0:
            raise ValueError("shard_count must be positive")
        if max_documents <= 0 or segment_documents <= 0 or segment_source_bytes <= 0:
            raise ValueError("document and segment limits must be positive")
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".rebuild-", dir=root) as temporary:  # noqa: SIM117
            with closing(sqlite3.connect(Path(temporary) / "snapshot.sqlite3")) as snapshot:
                snapshot.execute("PRAGMA cache_size=-8192")
                snapshot.execute("PRAGMA temp_store=FILE")
                snapshot.execute("CREATE TABLE documents(id BLOB PRIMARY KEY, partition INTEGER, payload TEXT)")
                snapshot.execute("CREATE TABLE pending(id BLOB PRIMARY KEY, crawled_at TEXT)")
                snapshot.execute("CREATE TABLE nodes(id BLOB PRIMARY KEY, shard TEXT)")
                snapshot.execute("CREATE INDEX document_partition ON documents(partition, id)")
                pending_count = 0
                for document in self.documents.iter_pending_for_index(limit=max_documents + 1):
                    pending_count += 1
                    if pending_count > max_documents:
                        raise ValueError("pending crawl documents exceed max_documents")
                    _stage_document(snapshot, document, shard_count)
                    snapshot.execute("INSERT INTO pending VALUES (?, ?)", (document.url_id.value, document.crawled_at))
                if not pending_count:
                    raise ValueError("no crawled documents are awaiting indexing")
                retained = _prepare_previous(root, snapshot, shard_count, backend, target_shard_payload_bytes, full_rebuild)
                snapshot.execute("INSERT OR IGNORE INTO nodes(id) SELECT id FROM documents")
                snapshot.commit()
                document_count = snapshot.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                scores = authority_scores if authority_scores is not None else _pagerank_scores(self.documents, snapshot)
                manifest = publish_lexical_generation(
                    root,
                    generation,
                    ((f"u-{blake3(generation.encode()).hexdigest()[:12]}-{name}", items)
                     for name, items in _staged_segments(snapshot, segment_documents, segment_source_bytes)),
                    authority_scores=scores,
                    target_shard_payload_bytes=target_shard_payload_bytes,
                    backend=backend,
                    signer=signer,
                    retained_segments=retained,
                )
                cursor = snapshot.execute("SELECT id, crawled_at FROM pending ORDER BY id")
                while versions := cursor.fetchmany(500):
                    self.documents.mark_indexed_versions(
                        ((UrlId(bytes(identifier)), timestamp) for identifier, timestamp in versions), manifest.generation,
                        transition_candidates=True,
                    )
        lexical = [artifact for artifact in manifest.artifacts if artifact.header.shard_type in {"lexical_index", "tantivy_index"}]
        reused_count = sum(artifact.source_generation is not None for artifact in lexical)
        return IndexBuildResult(
            manifest,
            document_count,
            len(lexical), reused_count, len(lexical) - reused_count,
        )


def _prepare_previous(root, snapshot, shard_count, backend, target_bytes, full_rebuild=False) -> tuple[RetainedSegment, ...]:
    if not (root / "active.json").exists():
        return ()
    store = ManifestStore(root, require_signatures=False)
    manifest = store.active_manifest()
    directory = store.generations / manifest.generation
    indexes = [artifact for artifact in manifest.artifacts if artifact.header.shard_type in {"lexical_index", "tantivy_index"}]
    catalogs = {artifact.header.shard_id: artifact for artifact in manifest.artifacts if artifact.header.shard_type == "index_catalog"}
    if any(index.header.shard_id not in catalogs for index in indexes):
        # One-time migration for generations published before source inventories.
        for index in indexes:
            for document in _artifact_documents(directory, index):
                _stage_document(snapshot, document, shard_count, replace=False)
        return ()
    previous = tuple(RetainedSegment(directory, index, catalogs[index.header.shard_id]) for index in indexes)
    incompatible = set()
    for segment in previous:
        routing, identifiers, version = load_segment_catalog(segment)
        if routing.summary_version < 2:
            incompatible.add(segment.index.header.shard_id)
        if segment.index.header.shard_type == "tantivy_index" and version != tantivy_capability().version:
            incompatible.add(segment.index.header.shard_id)
        snapshot.executemany("INSERT INTO nodes VALUES (?, ?)",
                             ((bytes.fromhex(identifier), segment.index.header.shard_id) for identifier in identifiers))
    affected = {row[0] for row in snapshot.execute("SELECT DISTINCT nodes.shard FROM nodes JOIN pending ON nodes.id=pending.id")}
    expected_type = "tantivy_index" if backend == "tantivy" else "lexical_index"
    retained = []
    for segment in previous:
        index = segment.index
        if (full_rebuild or index.header.shard_id in affected or index.header.shard_id in incompatible or index.header.shard_type != expected_type
                or (target_bytes is not None and index.header.payload_length > target_bytes)):
            for document in _artifact_documents(directory, index):
                _stage_document(snapshot, document, shard_count, replace=False)
        else:
            retained.append(segment)
    return tuple(retained)


def _artifact_documents(directory: Path, artifact: ShardArtifact) -> Iterator[SearchDocument]:
    header, payload = read_shard(directory / artifact.relative_path)
    if header != artifact.header:
        raise ValueError("previous shard changed after validation")
    yield from native_documents(payload) if header.shard_type == "tantivy_index" else documents_from_payload(payload)


def _pagerank_scores(documents: CrawledDocumentRepository, snapshot: sqlite3.Connection) -> Iterator[dict[str, float]]:
    rank = DiskPageRank(snapshot)
    rank.add_nodes(UrlId(bytes(row[0])) for row in snapshot.execute("SELECT id FROM nodes"))
    cursor = snapshot.execute("SELECT id FROM nodes ORDER BY id")
    while rows := cursor.fetchmany(500):
        rank.add_edges(documents.iter_link_edges(UrlId(bytes(row[0])) for row in rows))
        snapshot.commit()
    rank.compute()
    yield from rank.score_batches()


def _stage_document(snapshot: sqlite3.Connection, document: SearchDocument, shard_count: int, *, replace: bool = True) -> None:
    record = asdict(document)
    record["url_id"] = document.url_id.hex
    partition = int.from_bytes(blake3(document.url_id.value).digest(length=8), "big") % shard_count
    conflict = "REPLACE" if replace else "IGNORE"
    snapshot.execute(f"INSERT OR {conflict} INTO documents VALUES (?, ?, ?)",
                     (document.url_id.value, partition, json.dumps(record, separators=(",", ":"))))


def _staged_segments(
    snapshot: sqlite3.Connection, document_limit: int, byte_limit: int,
) -> Iterator[tuple[str, tuple[SearchDocument, ...]]]:
    """Stream deterministic chunks without materializing a complete hash partition."""
    documents: list[SearchDocument] = []
    size = 0
    partition = None
    chunk = 0
    for current, payload in snapshot.execute("SELECT partition, payload FROM documents ORDER BY partition, id"):
        source_bytes = len(payload.encode("utf-8"))
        if source_bytes > byte_limit:
            raise ValueError("document exceeds segment_source_bytes")
        if documents and (current != partition or len(documents) >= document_limit or size + source_bytes > byte_limit):
            yield f"shard-{partition:04d}-batch-{chunk:06d}", tuple(documents)
            documents = []
            size = 0
            chunk += 1
        if current != partition:
            chunk = 0
            partition = current
        record = json.loads(payload)
        record["url_id"] = UrlId(bytes.fromhex(record["url_id"]))
        documents.append(SearchDocument(**record))
        size += source_bytes
    if documents:
        yield f"shard-{partition:04d}-batch-{chunk:06d}", tuple(documents)
