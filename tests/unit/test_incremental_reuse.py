import errno
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from search_engine.canonicalization import canonicalize_url
from search_engine.crawl_processing import CrawlResultProcessor
from search_engine.crawl_store import CrawledDocumentRepository
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.manifest import Manifest, ManifestStore
from search_engine.publisher import load_active_search_service, publish_lexical_generation
from search_engine.security import ReleaseSigner
from search_engine.storage import ArtifactValidationError, ShardArtifact, write_shard

from search_engine.engine.index_builder import LocalIndexBuilder


@pytest.mark.parametrize("copy_fallback", (False, True))
def test_unchanged_shards_are_not_decoded_and_remain_self_contained(tmp_path, monkeypatch, copy_fallback):
    from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState

    import search_engine.engine.index_builder as builder
    from search_engine.engine import publisher

    signer = ReleaseSigner("test-key", Ed25519PrivateKey.generate())
    url = canonicalize_url("https://example.com/old")
    old = SearchDocument(UrlId.from_canonical_url(url), url.value, "Retained", "unchanged content")
    root = tmp_path / "artifacts"
    first = publish_lexical_generation(root, "first", {"original": (old,)}, signer=signer)
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    url_id = candidates.upsert(canonicalize_url("https://example.com/new"), "seed")
    candidates.transition(url_id, UrlState.LIVENESS_PENDING)
    candidates.transition(url_id, UrlState.LIVE)
    documents = CrawledDocumentRepository(database)
    documents.save(CrawlResultProcessor(candidates).process_html(url_id, "<title>Added</title><p>New content.</p>"))

    def forbid_decode(*args):
        pytest.fail("unchanged index must not be decoded")

    monkeypatch.setattr(builder, "_artifact_documents", forbid_decode)
    if copy_fallback:
        def no_links(*args):
            raise OSError(errno.EXDEV, "cross-device link")
        monkeypatch.setattr(publisher.os, "link", no_links)
    result = LocalIndexBuilder(documents, candidates).build(root, "second", max_documents=1, signer=signer)
    assert result.reused_shards == 1
    retained = next(item for item in result.manifest.artifacts if item.header.shard_id == "original" and item.header.shard_type == "lexical_index")
    original = next(item for item in first.artifacts if item.header.shard_type == "lexical_index")
    assert retained.header == original.header
    assert retained.source_generation == "first"
    old_path = root / "generations/first" / original.relative_path
    new_path = root / "generations/second" / retained.relative_path
    assert old_path.samefile(new_path) is not copy_fallback
    # No cross-generation file dependency: moving the old directory does not
    # prevent signed validation, serving, or later retention of the second one.
    (root / "generations/first").rename(root / "archived-first")
    service = load_active_search_service(root, verifier=signer.verifier())
    assert service.search("unchanged").results[0].hit.url_id == old.url_id
    assert service.search("added").results[0].hit.url_id == url_id


def test_legacy_generation_migrates_to_catalogs(tmp_path):
    from search_engine.state import CandidateRepository, CrawlStateDatabase, UrlState

    root = tmp_path / "artifacts"
    url = canonicalize_url("https://example.com/legacy")
    old = SearchDocument(UrlId.from_canonical_url(url), url.value, "Legacy", "preserved")
    from search_engine.index import Bm25fIndex

    header = write_shard(root / "generations/old/indexes/old.shard", Bm25fIndex((old,)).to_payload(),
                         shard_type="lexical_index", shard_id="old", generation="old")
    store = ManifestStore(root, require_signatures=False)
    store.write_generation(replace(Manifest.create("old", (ShardArtifact("indexes/old.shard", header),)), format_version=1))
    store.activate("old")
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    new_id = candidates.upsert(canonicalize_url("https://example.com/new"), "seed")
    candidates.transition(new_id, UrlState.LIVENESS_PENDING)
    candidates.transition(new_id, UrlState.LIVE)
    documents = CrawledDocumentRepository(database)
    documents.save(CrawlResultProcessor(candidates).process_html(new_id, "<title>New</title><p>Added.</p>"))
    result = LocalIndexBuilder(documents, candidates).build(root, "new", max_documents=1)
    assert result.document_count == 2
    assert result.reused_shards == 0
    assert any(item.header.shard_type == "index_catalog" for item in result.manifest.artifacts)
    assert load_active_search_service(root, require_signatures=False).search("legacy").results


def test_reused_origin_is_explicit_and_rejected_in_old_manifest_format(tmp_path):
    header = write_shard(tmp_path / "generations/next/indexes/old.shard", b"payload",
                         shard_type="document", shard_id="old", generation="old")
    store = ManifestStore(tmp_path, require_signatures=False)
    artifact = ShardArtifact("indexes/old.shard", header, source_generation="old")
    store.write_generation(replace(Manifest.create("next", (artifact,)), format_version=1))
    with pytest.raises(ArtifactValidationError, match="format 2"):
        store.activate("next")
