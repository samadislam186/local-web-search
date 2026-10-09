import base64
import errno
import os
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from search_engine.manifest import Manifest, ManifestStore
from search_engine.security import DetachedSignature, PinnedKeyVerifier
from search_engine.storage import (
    ArtifactValidationError,
    ShardArtifact,
    iter_shard_payload,
    read_shard,
    read_shard_header,
    safe_relative_path,
    safe_shard_relative_path,
    write_shard,
    write_shard_stream,
)

import search_engine.engine.manifest as manifest_module


def _published_generation(root: Path, generation: str = "g000001") -> ManifestStore:
    store = ManifestStore(root, require_signatures=False)
    shard_path = store.generations / generation / "documents" / "part-0001.shard"
    header = write_shard(shard_path, b"search data", shard_type="document", shard_id="part-0001", generation=generation)
    store.write_generation(Manifest.create(generation, (ShardArtifact("documents/part-0001.shard", header),)))
    return store


def test_shard_round_trip_and_generation_activation(tmp_path: Path) -> None:
    store = _published_generation(tmp_path)
    store.activate("g000001")
    assert store.active_manifest().generation == "g000001"
    header, payload = read_shard(tmp_path / "generations/g000001/documents/part-0001.shard")
    assert header.shard_type == "document"
    assert payload == b"search data"


def test_manifest_records_dependency_versions_and_source_crawl_provenance(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, require_signatures=False)
    header = write_shard(
        store.generations / "g1/documents/part.shard",
        b"content",
        shard_type="document",
        shard_id="part",
        generation="g1",
        referenced_versions={"canonicalization": 1, "url_id": 1},
    )
    manifest = Manifest.create(
        "g1",
        (ShardArtifact("documents/part.shard", header),),
        dependencies={"canonicalization": 1, "url_id": 1},
        source_crawls=("CC-MAIN-2026-01",),
    )
    store.write_generation(manifest)
    activated = store.activate("g1")
    assert activated.dependencies == {"canonicalization": 1, "url_id": 1}
    assert activated.source_crawls == ("CC-MAIN-2026-01",)


def test_manifest_rejects_artifact_with_unlisted_dependency(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, require_signatures=False)
    header = write_shard(
        store.generations / "g1/documents/part.shard",
        b"content",
        shard_type="document",
        shard_id="part",
        generation="g1",
        referenced_versions={"parser": 2},
    )
    store.write_generation(Manifest.create("g1", (ShardArtifact("documents/part.shard", header),)))
    with pytest.raises(ArtifactValidationError, match="dependency mismatch"):
        store.activate("g1")


def test_shard_reader_enforces_payload_bound_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "bounded.shard"
    write_shard(path, b"search data", shard_type="document", shard_id="bounded", generation="g1")
    with pytest.raises(ArtifactValidationError, match="maximum size"):
        read_shard(path, max_payload_bytes=1)


def test_streaming_shard_writer_and_reader_round_trip_with_small_chunks(tmp_path: Path) -> None:
    path = tmp_path / "streamed.shard"
    header = write_shard_stream(
        path,
        (chunk for chunk in (b"search ", b"data ", b"in chunks")),
        shard_type="document",
        shard_id="streamed",
        generation="g1",
        referenced_versions={"canonicalization": 1, "url_id": 1},
    )
    assert read_shard_header(path) == header
    assert header.referenced_versions == {"canonicalization": 1, "url_id": 1}
    assert b"".join(iter_shard_payload(path, chunk_size=3)) == b"search data in chunks"


def test_shard_writer_rejects_invalid_referenced_versions(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        write_shard(
            tmp_path / "invalid.shard",
            b"content",
            shard_type="document",
            shard_id="invalid",
            generation="g1",
            referenced_versions={"url_id": 0},
        )


def test_streaming_shard_reader_rejects_trailing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "trailing.shard"
    write_shard(path, b"content", shard_type="document", shard_id="trailing", generation="g1")
    with path.open("ab") as file:
        file.write(b"unexpected")
    with pytest.raises(ArtifactValidationError, match="trailing"):
        list(iter_shard_payload(path))


def test_bad_generation_never_replaces_active_generation(tmp_path: Path) -> None:
    store = _published_generation(tmp_path)
    store.activate("g000001")
    bad = _published_generation(tmp_path, "g000002")
    shard = bad.generations / "g000002/documents/part-0001.shard"
    shard.write_bytes(b"corrupt")
    with pytest.raises(ArtifactValidationError):
        store.activate("g000002")
    assert store.active_manifest().generation == "g000001"


@pytest.mark.parametrize("missing", ("manifest.json", "documents/part-0001.shard"))
def test_missing_next_generation_file_preserves_active_generation(
    tmp_path: Path, missing: str
) -> None:
    store = _published_generation(tmp_path, "g1")
    _published_generation(tmp_path, "g2")
    store.activate("g1")
    # Simulate an incomplete transfer using only files created by this fixture.
    (store.generations / "g2" / missing).unlink()
    with pytest.raises(ArtifactValidationError):
        store.activate("g2")
    assert store.active_manifest().generation == "g1"


def test_corrupt_active_pointer_can_be_recovered_by_explicit_validated_rollback(tmp_path: Path) -> None:
    store = _published_generation(tmp_path, "g1")
    store.activate("g1")
    store.active_path.write_bytes(b"incomplete pointer")
    with pytest.raises(ArtifactValidationError, match="active generation"):
        store.active_manifest()
    assert store.rollback("g1").generation == "g1"
    assert store.active_manifest().generation == "g1"


def test_generation_rollback_and_interrupted_publication_inspection(tmp_path: Path) -> None:
    store = _published_generation(tmp_path, "g1")
    _published_generation(tmp_path, "g2")
    store.activate("g1")
    store.activate("g2")
    assert store.rollback("g1").generation == "g1"
    interrupted = store.generations / "g3" / "indexes" / "partial.shard"
    interrupted.parent.mkdir(parents=True)
    interrupted.write_bytes(b"incomplete")
    assert store.incomplete_generations() == ("g3",)
    assert store.active_manifest().generation == "g1"


def test_failed_activation_keeps_prior_generation_and_removes_temporary_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _published_generation(tmp_path, "g1")
    _published_generation(tmp_path, "g2")
    store.activate("g1")
    original_replace = os.replace

    def fail_active_pointer(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == store.active_path:
            raise OSError("disk full")
        original_replace(source, destination)

    monkeypatch.setattr(manifest_module.os, "replace", fail_active_pointer)
    with pytest.raises(ArtifactValidationError, match="atomically write"):
        store.activate("g2")

    assert store.active_manifest().generation == "g1"
    assert not tuple(store.root.glob(".active.json.*"))


def test_disk_full_during_pointer_flush_preserves_active_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _published_generation(tmp_path, "g1")
    _published_generation(tmp_path, "g2")
    store.activate("g1")

    def disk_full(_descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(manifest_module.os, "fsync", disk_full)
    with pytest.raises(ArtifactValidationError, match="No space left"):
        store.activate("g2")
    assert store.active_manifest().generation == "g1"
    assert not tuple(store.root.glob(".active.json.*"))


@pytest.mark.parametrize("version_field", ("format_version", "canonicalization_version", "url_id_version"))
def test_incompatible_manifest_preserves_prior_active_generation(tmp_path: Path, version_field: str) -> None:
    store = _published_generation(tmp_path, "g1")
    store.activate("g1")
    manifest = replace(Manifest.create("g2", ()), **{version_field: 999})
    store.write_generation(manifest)
    with pytest.raises(ArtifactValidationError):
        store.activate("g2")
    assert store.active_manifest().generation == "g1"


def test_generation_manifest_cannot_be_rewritten_with_different_artifacts(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, require_signatures=False)
    first = write_shard(
        store.generations / "g1/indexes/first.shard", b"first", shard_type="index", shard_id="first", generation="g1"
    )
    store.write_generation(Manifest.create("g1", (ShardArtifact("indexes/first.shard", first),)))
    second = write_shard(
        store.generations / "g1/indexes/second.shard", b"second", shard_type="index", shard_id="second", generation="g1"
    )
    with pytest.raises(FileExistsError, match="different manifest"):
        store.write_generation(Manifest.create("g1", (ShardArtifact("indexes/second.shard", second),)))


def test_generation_identifier_cannot_escape_artifact_root(tmp_path: Path) -> None:
    with pytest.raises(ArtifactValidationError):
        ManifestStore(tmp_path, require_signatures=False).write_generation(Manifest.create("../../outside", ()))


def test_windows_rooted_artifact_path_is_rejected() -> None:
    with pytest.raises(ArtifactValidationError):
        safe_relative_path("C:\\outside\\artifact.shard")


@pytest.mark.parametrize("path", ("documents/payload.pickle", "documents/table.parquet", "documents/raw.bin"))
def test_manifest_artifacts_refuse_executable_and_unsupported_binary_formats(path: str) -> None:
    with pytest.raises(ArtifactValidationError, match="unsupported artifact file format"):
        safe_shard_relative_path(path)


def test_manifest_refuses_executable_artifact_extension_before_payload_read(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, require_signatures=False)
    header = write_shard(
        store.generations / "g1/documents/data.pickle",
        b"not executable",
        shard_type="document",
        shard_id="data",
        generation="g1",
    )
    store.write_generation(Manifest.create("g1", (ShardArtifact("documents/data.pickle", header),)))
    with pytest.raises(ArtifactValidationError, match="unsupported artifact file format"):
        store.activate("g1")


def test_generation_diff_identifies_incremental_artifacts(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path, require_signatures=False)
    first_header = write_shard(
        store.generations / "g1/indexes/stable.shard", b"stable", shard_type="index", shard_id="stable", generation="g1"
    )
    old_header = write_shard(
        store.generations / "g1/indexes/old.shard", b"old", shard_type="index", shard_id="old", generation="g1"
    )
    previous_changed_header = write_shard(
        store.generations / "g1/indexes/changed.shard", b"old changed", shard_type="index", shard_id="changed", generation="g1"
    )
    store.write_generation(Manifest.create("g1", (ShardArtifact("indexes/stable.shard", first_header), ShardArtifact("indexes/old.shard", old_header), ShardArtifact("indexes/changed.shard", previous_changed_header))))
    second_header = write_shard(
        store.generations / "g2/indexes/stable.shard", b"stable", shard_type="index", shard_id="stable", generation="g2"
    )
    changed_header = write_shard(
        store.generations / "g2/indexes/changed.shard", b"changed", shard_type="index", shard_id="changed", generation="g2"
    )
    new_header = write_shard(
        store.generations / "g2/indexes/new.shard", b"new", shard_type="index", shard_id="new", generation="g2"
    )
    store.write_generation(Manifest.create("g2", (ShardArtifact("indexes/stable.shard", second_header), ShardArtifact("indexes/changed.shard", changed_header), ShardArtifact("indexes/new.shard", new_header))))
    diff = store.diff_generations("g1", "g2")
    assert diff.unchanged == ("indexes/stable.shard",)
    assert diff.new == ("indexes/new.shard",)
    assert diff.changed == ("indexes/changed.shard",)
    assert diff.removed == ("indexes/old.shard",)
    plan = store.plan_artifact_download("g1", "g2")
    assert plan.generation == "g2"
    assert tuple(artifact.relative_path for artifact in plan.required) == ("indexes/changed.shard", "indexes/new.shard")
    assert plan.retained == ("indexes/stable.shard",)
    assert plan.removed == ("indexes/old.shard",)
    assert store.active_path.exists() is False


def test_activation_requires_valid_pinned_manifest_signature(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = PinnedKeyVerifier({"release": base64.b64encode(public).decode("ascii")})
    store = ManifestStore(tmp_path, verifier=verifier)
    header = write_shard(
        store.generations / "g1/indexes/part.shard", b"content", shard_type="index", shard_id="part", generation="g1"
    )
    manifest = Manifest.create("g1", (ShardArtifact("indexes/part.shard", header),))
    store.write_generation(manifest)
    with pytest.raises(ArtifactValidationError, match="signature"):
        store.activate("g1")
    signature = DetachedSignature("release", base64.b64encode(private.sign((store.generations / "g1/manifest.json").read_bytes())).decode("ascii"))
    store.write_generation(manifest, signature)
    assert store.activate("g1").generation == "g1"
