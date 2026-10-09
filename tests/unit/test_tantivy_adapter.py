import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation
from search_engine.security import ReleaseSigner
from search_engine.storage import ArtifactValidationError
from search_engine.tantivy_adapter import (
    TantivyIndex,
    TantivyUnavailableError,
    require_tantivy,
    tantivy_capability,
)


def test_tantivy_capability_is_explicit_when_optional_dependency_is_absent() -> None:
    capability = tantivy_capability()
    if capability.available:
        pytest.skip("Tantivy is installed in this environment")
    assert capability.reason == "optional dependency 'tantivy' is not installed"
    with pytest.raises(TantivyUnavailableError, match="not installed"):
        require_tantivy()


def test_tantivy_build_open_and_query_round_trip(tmp_path) -> None:
    if not tantivy_capability().available:
        pytest.skip("Tantivy is not installed in this environment")
    canonical = canonicalize_url("https://example.com/python")
    document = SearchDocument(
        UrlId.from_canonical_url(canonical),
        canonical.value,
        "Python crawler",
        "local search",
        host="example.com",
        domain="example.com",
    )
    built = TantivyIndex.build(tmp_path / "tantivy", (document,))
    assert built.search("python", 1)[0].url_id == document.url_id
    reopened = TantivyIndex.open(tmp_path / "tantivy")
    assert reopened.search("crawler", 1)[0].url == canonical.value
    assert reopened.search("example.com", 1)[0].url_id == document.url_id


def test_native_generation_publishes_and_serves_through_the_manifest(tmp_path) -> None:
    if not tantivy_capability().available:
        pytest.skip("Tantivy is not installed")
    canonical = canonicalize_url("https://example.com/python")
    document = SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python", "native crawler")
    signer = ReleaseSigner("release-test", Ed25519PrivateKey.generate())
    manifest = publish_lexical_generation(tmp_path, "native-g1", {"s1": (document,)}, backend="tantivy", signer=signer)
    assert any(item.header.shard_type == "tantivy_index" for item in manifest.artifacts)
    service = load_active_search_service(tmp_path, verifier=signer.verifier())
    assert service.stats()["loaded_shards"] == 0
    assert service.search("python").results[0].hit.url_id == document.url_id
    assert service.stats()["loaded_shards"] == 1


def test_native_package_rejects_traversal_before_extraction() -> None:
    payload = json.dumps({"format_version": 1, "backend_version": "0.26.2", "documents": [], "files": {"meta.json": "", "../escape": ""}}).encode()
    with pytest.raises(ArtifactValidationError, match="native index package"):
        TantivyIndex.from_payload(payload)
