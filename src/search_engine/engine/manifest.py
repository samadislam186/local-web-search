"""Versioned corpus manifests with validate-before-activate publication."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .security import DetachedSignature, PinnedKeyVerifier
from .storage import (
    ArtifactValidationError,
    ShardArtifact,
    ShardHeader,
    read_shard,
    safe_shard_relative_path,
)

MANIFEST_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Manifest:
    generation: str
    canonicalization_version: int
    url_id_version: int
    created_at: str
    artifacts: tuple[ShardArtifact, ...]
    format_version: int = MANIFEST_FORMAT_VERSION
    dependencies: dict[str, int] = field(default_factory=lambda: {"canonicalization": 1, "url_id": 1})
    source_crawls: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        generation: str,
        artifacts: tuple[ShardArtifact, ...],
        *,
        dependencies: dict[str, int] | None = None,
        source_crawls: tuple[str, ...] = (),
    ) -> Manifest:
        return cls(
            generation,
            1,
            1,
            datetime.now(UTC).isoformat(),
            artifacts,
            dependencies=dict(dependencies or {"canonicalization": 1, "url_id": 1}),
            source_crawls=source_crawls,
        )


@dataclass(frozen=True, slots=True)
class GenerationDiff:
    unchanged: tuple[str, ...]
    new: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactDownloadPlan:
    """Immutable transfer plan derived from two generation manifests."""

    generation: str
    required: tuple[ShardArtifact, ...]
    retained: tuple[str, ...]
    removed: tuple[str, ...]


class ManifestStore:
    def __init__(self, root: Path, *, verifier: PinnedKeyVerifier | None = None, require_signatures: bool = True) -> None:
        if require_signatures and verifier is None:
            raise ValueError("a pinned-key verifier is required for artifact activation")
        self.root = root
        self.generations = root / "generations"
        self.active_path = root / "active.json"
        self.verifier = verifier
        self.require_signatures = require_signatures

    def write_generation(self, manifest: Manifest, signature: DetachedSignature | None = None) -> Path:
        generation = safe_generation(manifest.generation)
        directory = self.generations / generation
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / "manifest.json"
        encoded_manifest = _json_bytes(_manifest_to_json(manifest))
        if manifest_path.exists():
            try:
                existing_manifest = manifest_path.read_bytes()
            except OSError as exc:
                raise ArtifactValidationError(f"cannot read existing manifest {manifest_path}: {exc}") from exc
            if existing_manifest != encoded_manifest:
                raise FileExistsError(f"generation already has a different manifest: {generation}")
        else:
            self._write_bytes_atomic(manifest_path, encoded_manifest)
        if signature is not None:
            signature_path = directory / "manifest.sig.json"
            encoded_signature = _json_bytes(asdict(signature))
            if signature_path.exists() and signature_path.read_bytes() != encoded_signature:
                raise FileExistsError(f"generation already has a different signature: {generation}")
            if not signature_path.exists():
                self._write_bytes_atomic(signature_path, encoded_signature)
        return manifest_path

    def load_generation(self, generation: str) -> Manifest:
        generation = safe_generation(generation)
        path = self.generations / generation / "manifest.json"
        try:
            data = json.loads(path.read_text("utf-8"))
            artifacts = tuple(
                ShardArtifact(relative_path=item["relative_path"], header=ShardHeader(**item["header"]))
                for item in data.pop("artifacts")
            )
            if "source_crawls" in data:
                data["source_crawls"] = tuple(data["source_crawls"])
            manifest = Manifest(artifacts=artifacts, **data)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError(f"invalid manifest {path}: {exc}") from exc
        return manifest

    def validate_generation(self, generation: str) -> Manifest:
        generation = safe_generation(generation)
        self._verify_manifest_signature(generation)
        manifest = self.load_generation(generation)
        if manifest.format_version != MANIFEST_FORMAT_VERSION or manifest.generation != generation:
            raise ArtifactValidationError("manifest generation or format mismatch")
        _validate_manifest_metadata(manifest)
        seen: set[str] = set()
        directory = self.generations / generation
        for artifact in manifest.artifacts:
            if artifact.relative_path in seen:
                raise ArtifactValidationError("duplicate artifact path")
            seen.add(artifact.relative_path)
            path = directory / safe_shard_relative_path(artifact.relative_path)
            header, _ = read_shard(path)
            if header != artifact.header or header.generation != generation:
                raise ArtifactValidationError(f"manifest/shard mismatch: {artifact.relative_path}")
            if any(
                manifest.dependencies.get(name) != version
                for name, version in header.referenced_versions.items()
            ):
                raise ArtifactValidationError(f"artifact dependency mismatch: {artifact.relative_path}")
        return manifest

    def activate(self, generation: str) -> Manifest:
        manifest = self.validate_generation(generation)
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(self.active_path, {"generation": manifest.generation})
        return manifest

    def active_manifest(self) -> Manifest:
        try:
            generation = json.loads(self.active_path.read_text("utf-8"))["generation"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("no valid active generation") from exc
        return self.validate_generation(str(generation))

    def rollback(self, generation: str) -> Manifest:
        """Atomically select a previously validated immutable generation."""
        return self.activate(generation)

    def incomplete_generations(self) -> tuple[str, ...]:
        """List unpublished generation directories left by an interrupted writer.

        The method is intentionally non-destructive: callers can inspect or
        clean only these never-activated directories according to retention.
        """
        if not self.generations.exists():
            return ()
        return tuple(
            directory.name
            for directory in sorted(self.generations.iterdir())
            if directory.is_dir() and not (directory / "manifest.json").is_file()
        )

    def _verify_manifest_signature(self, generation: str) -> None:
        if not self.require_signatures:
            return
        assert self.verifier is not None
        directory = self.generations / generation
        try:
            signature = DetachedSignature(**json.loads((directory / "manifest.sig.json").read_text("utf-8")))
            payload = (directory / "manifest.json").read_bytes()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("missing or invalid manifest signature") from exc
        self.verifier.verify(payload, signature)

    def diff_generations(self, previous_generation: str, next_generation: str) -> GenerationDiff:
        """Compare manifests by immutable artifact identity and checksum."""
        previous = {artifact.relative_path: artifact.header for artifact in self.load_generation(previous_generation).artifacts}
        next_artifacts = {artifact.relative_path: artifact.header for artifact in self.load_generation(next_generation).artifacts}
        unchanged = tuple(sorted(path for path in previous.keys() & next_artifacts.keys() if previous[path].checksum == next_artifacts[path].checksum))
        changed = tuple(sorted(path for path in previous.keys() & next_artifacts.keys() if previous[path].checksum != next_artifacts[path].checksum))
        return GenerationDiff(
            unchanged,
            tuple(sorted(next_artifacts.keys() - previous.keys())),
            changed,
            tuple(sorted(previous.keys() - next_artifacts.keys())),
        )

    def plan_artifact_download(self, previous_generation: str, next_generation: str) -> ArtifactDownloadPlan:
        """Select only new or changed immutable artifacts for a next generation."""
        previous = {artifact.relative_path: artifact for artifact in self.load_generation(previous_generation).artifacts}
        following_manifest = self.load_generation(next_generation)
        following = {artifact.relative_path: artifact for artifact in following_manifest.artifacts}
        required = tuple(
            following[path]
            for path in sorted(following)
            if path not in previous or previous[path].header.checksum != following[path].header.checksum
        )
        retained = tuple(
            path
            for path in sorted(following.keys() & previous.keys())
            if following[path].header.checksum == previous[path].header.checksum
        )
        return ArtifactDownloadPlan(
            following_manifest.generation,
            required,
            retained,
            tuple(sorted(previous.keys() - following.keys())),
        )

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        ManifestStore._write_bytes_atomic(path, _json_bytes(value))

    @staticmethod
    def _write_bytes_atomic(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
                temporary = Path(file.name)
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise ArtifactValidationError(f"cannot atomically write {path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _manifest_to_json(manifest: Manifest) -> dict[str, object]:
    data = asdict(manifest)
    data["artifacts"] = [
        {"relative_path": artifact.relative_path, "header": asdict(artifact.header)}
        for artifact in manifest.artifacts
    ]
    return data


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_generation(value: str) -> str:
    if not isinstance(value, str) or not _GENERATION.fullmatch(value) or value in {".", ".."}:
        raise ArtifactValidationError(f"unsafe generation identifier: {value!r}")
    return value


def _validate_manifest_metadata(manifest: Manifest) -> None:
    if manifest.canonicalization_version != 1 or manifest.url_id_version != 1:
        raise ArtifactValidationError("unsupported manifest identity versions")
    if not manifest.dependencies or any(
        not isinstance(name, str) or not name or not isinstance(version, int) or isinstance(version, bool) or version <= 0
        for name, version in manifest.dependencies.items()
    ):
        raise ArtifactValidationError("invalid manifest dependencies")
    if len(set(manifest.source_crawls)) != len(manifest.source_crawls) or any(
        not isinstance(crawl, str) or not crawl.strip() for crawl in manifest.source_crawls
    ):
        raise ArtifactValidationError("invalid manifest source crawls")
    try:
        created_at = datetime.fromisoformat(manifest.created_at)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError("invalid manifest creation time") from exc
    if created_at.tzinfo is None:
        raise ArtifactValidationError("manifest creation time must be timezone-aware")
