"""Bounded local index builder from durable crawl documents to a published generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blake3 import blake3

from .crawl_store import CrawledDocumentRepository
from .graph import HyperlinkGraph
from .index import Bm25fIndex, SearchDocument
from .manifest import Manifest, ManifestStore
from .pagerank import compute_pagerank
from .publisher import publish_lexical_generation
from .state import CandidateRepository, UrlState
from .storage import read_shard


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    manifest: Manifest
    document_count: int
    shard_count: int


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
    ) -> IndexBuildResult:
        if shard_count <= 0:
            raise ValueError("shard_count must be positive")
        batch = self.documents.pending_for_index(limit=max_documents + 1)
        if not batch:
            raise ValueError("no crawled documents are awaiting indexing")
        if len(batch) > max_documents:
            raise ValueError("pending crawl documents exceed max_documents")
        snapshot = {document.url_id.hex: document for document in _active_documents(root)}
        snapshot.update({document.url_id.hex: document for document in batch})
        segments = _partition(tuple(snapshot.values()), shard_count)
        scores = authority_scores if authority_scores is not None else _pagerank_scores(self.documents, tuple(snapshot.values()))
        manifest = publish_lexical_generation(
            root,
            generation,
            segments,
            authority_scores=scores,
            target_shard_payload_bytes=target_shard_payload_bytes,
        )
        self.documents.mark_indexed((document.url_id for document in batch), manifest.generation)
        for document in batch:
            if self.candidates.get_state(document.url_id) is UrlState.CRAWLED:
                self.candidates.transition(document.url_id, UrlState.INDEXED, f"generation:{manifest.generation}")
        return IndexBuildResult(
            manifest,
            len(snapshot),
            sum(artifact.header.shard_type == "lexical_index" for artifact in manifest.artifacts),
        )


def _partition(documents: tuple[SearchDocument, ...], shard_count: int) -> dict[str, tuple[SearchDocument, ...]]:
    segments: dict[str, list[SearchDocument]] = {}
    for document in documents:
        shard_number = int.from_bytes(blake3(document.url_id.value).digest(length=8), "big") % shard_count
        segments.setdefault(f"shard-{shard_number:04d}", []).append(document)
    return {shard_id: tuple(items) for shard_id, items in sorted(segments.items())}


def _active_documents(root: Path) -> tuple[SearchDocument, ...]:
    if not (root / "active.json").exists():
        return ()
    store = ManifestStore(root, require_signatures=False)
    manifest = store.active_manifest()
    documents: list[SearchDocument] = []
    for artifact in manifest.artifacts:
        if artifact.header.shard_type != "lexical_index":
            continue
        _header, payload = read_shard(store.generations / manifest.generation / artifact.relative_path)
        documents.extend(Bm25fIndex.from_payload(payload).documents)
    return tuple(documents)


def _pagerank_scores(documents: CrawledDocumentRepository, snapshot: tuple[SearchDocument, ...]) -> dict[str, float]:
    nodes = [document.url_id for document in snapshot]
    node_ids = {node.hex for node in nodes}
    edges = [edge for edge in documents.link_edges(nodes) if edge[1].hex in node_ids]
    graph = HyperlinkGraph.from_url_edges(edges, nodes=nodes)
    return compute_pagerank(graph).by_url_id(graph)
