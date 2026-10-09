"""Immutable, checksumed shard envelopes and atomic filesystem publication."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from blake3 import blake3

from .errors import SearchEngineError

SHARD_MAGIC = b"OSSHARD1"
SHARD_FORMAT_VERSION = 1
_PREFIX = struct.Struct(">8sI")
MAX_SHARD_PAYLOAD_BYTES = 512 * 1024 * 1024


class ArtifactValidationError(SearchEngineError):
    """Raised when an artifact is incomplete, malformed, or incompatible."""


@dataclass(frozen=True, slots=True)
class ShardHeader:
    format_version: int
    shard_type: str
    shard_id: str
    generation: str
    payload_length: int
    checksum: str
    creation_time: str
    compression: str = "none"
    referenced_versions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShardArtifact:
    relative_path: str
    header: ShardHeader
    source_generation: str | None = None


def write_shard(
    path: Path,
    payload: bytes,
    *,
    shard_type: str,
    shard_id: str,
    generation: str,
    compression: str = "none",
    referenced_versions: Mapping[str, int] | None = None,
) -> ShardHeader:
    """Write a shard to a temporary sibling then atomically replace its target."""
    return write_shard_stream(
        path,
        (payload,),
        shard_type=shard_type,
        shard_id=shard_id,
        generation=generation,
        compression=compression,
        referenced_versions=referenced_versions,
    )


def write_shard_stream(
    path: Path,
    chunks: Iterable[bytes],
    *,
    shard_type: str,
    shard_id: str,
    generation: str,
    compression: str = "none",
    referenced_versions: Mapping[str, int] | None = None,
) -> ShardHeader:
    """Atomically publish a bounded shard without materializing its payload in RAM."""
    _validate_identifier(shard_type, "shard_type")
    _validate_identifier(shard_id, "shard_id")
    _validate_identifier(generation, "generation")
    if compression != "none":
        raise ValueError("compression codecs are not implemented yet")
    normalized_versions = _normalize_referenced_versions(referenced_versions)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_temporary: Path | None = None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.payload.", delete=False) as file:
            payload_temporary = Path(file.name)
            digest = blake3()
            payload_length = 0
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("shard stream chunks must be bytes")
                payload_length += len(chunk)
                if payload_length > MAX_SHARD_PAYLOAD_BYTES:
                    raise ValueError("shard payload exceeds maximum size")
                digest.update(chunk)
                file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
        header = ShardHeader(
            format_version=SHARD_FORMAT_VERSION,
            shard_type=shard_type,
            shard_id=shard_id,
            generation=generation,
            payload_length=payload_length,
            checksum=digest.hexdigest(),
            creation_time=datetime.now(UTC).isoformat(),
            compression=compression,
            referenced_versions=normalized_versions,
        )
        encoded_header = json.dumps(asdict(header), sort_keys=True, separators=(",", ":")).encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
            temporary = Path(file.name)
            file.write(_PREFIX.pack(SHARD_MAGIC, len(encoded_header)))
            file.write(encoded_header)
            with payload_temporary.open("rb") as payload_file:
                while chunk := payload_file.read(64 * 1024):
                    file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        return header
    finally:
        if payload_temporary is not None:
            payload_temporary.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_shard(path: Path, *, max_payload_bytes: int = MAX_SHARD_PAYLOAD_BYTES) -> tuple[ShardHeader, bytes]:
    """Read and fully verify an immutable shard envelope."""
    header = read_shard_header(path, max_payload_bytes=max_payload_bytes)
    return header, b"".join(iter_shard_payload(path, max_payload_bytes=max_payload_bytes))


def read_shard_header(path: Path, *, max_payload_bytes: int = MAX_SHARD_PAYLOAD_BYTES) -> ShardHeader:
    """Read and validate just a shard envelope header without its payload."""
    if max_payload_bytes <= 0:
        raise ValueError("max_payload_bytes must be positive")
    try:
        with path.open("rb") as file:
            header, _header_end = _read_header(file, path, max_payload_bytes)
    except (OSError, struct.error) as exc:
        raise ArtifactValidationError(f"cannot read shard {path}: {exc}") from exc
    return header


def iter_shard_payload(
    path: Path, *, max_payload_bytes: int = MAX_SHARD_PAYLOAD_BYTES, chunk_size: int = 64 * 1024
) -> Iterator[bytes]:
    """Yield and verify a shard payload using bounded memory."""
    if max_payload_bytes <= 0 or chunk_size <= 0:
        raise ValueError("max_payload_bytes and chunk_size must be positive")
    try:
        with path.open("rb") as file:
            header, _header_end = _read_header(file, path, max_payload_bytes)
            remaining = header.payload_length
            digest = blake3()
            while remaining:
                chunk = file.read(min(remaining, chunk_size))
                if not chunk:
                    raise ArtifactValidationError(f"truncated shard payload: {path}")
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            if file.read(1):
                raise ArtifactValidationError(f"unexpected trailing data in shard: {path}")
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read shard {path}: {exc}") from exc
    if digest.hexdigest() != header.checksum:
        raise ArtifactValidationError(f"shard checksum mismatch: {path}")


def _read_header(file, path: Path, max_payload_bytes: int) -> tuple[ShardHeader, int]:
    prefix = file.read(_PREFIX.size)
    magic, header_length = _PREFIX.unpack(prefix)
    if magic != SHARD_MAGIC:
        raise ArtifactValidationError(f"invalid shard magic: {path}")
    if not 2 <= header_length <= 64 * 1024:
        raise ArtifactValidationError(f"invalid shard header length: {path}")
    raw_header = file.read(header_length)
    try:
        header = ShardHeader(**json.loads(raw_header))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"invalid shard header: {path}") from exc
    if header.format_version != SHARD_FORMAT_VERSION:
        raise ArtifactValidationError(f"unsupported shard format: {header.format_version}")
    if not 0 <= header.payload_length <= max_payload_bytes:
        raise ArtifactValidationError(f"shard payload exceeds maximum size: {path}")
    try:
        _validate_identifier(header.shard_type, "shard_type")
        _validate_identifier(header.shard_id, "shard_id")
        _validate_identifier(header.generation, "generation")
        _normalize_referenced_versions(header.referenced_versions)
        if header.compression != "none":
            raise ValueError("unsupported compression")
        if len(header.checksum) != 64 or any(char not in "0123456789abcdef" for char in header.checksum):
            raise ValueError("invalid checksum")
        creation_time = datetime.fromisoformat(header.creation_time)
        if creation_time.tzinfo is None:
            raise ValueError("creation time must be timezone-aware")
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"invalid shard header: {path}") from exc
    return header, _PREFIX.size + header_length


def safe_relative_path(value: str) -> Path:
    if "\\" in value:
        raise ArtifactValidationError(f"unsafe artifact path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
        raise ArtifactValidationError(f"unsafe artifact path: {value!r}")
    native = Path(*path.parts)
    if native.is_absolute() or native.drive:
        raise ArtifactValidationError(f"unsafe artifact path: {value!r}")
    return native


def safe_shard_relative_path(value: str) -> Path:
    """Allow only the project-owned bounded shard format in artifact manifests."""
    path = safe_relative_path(value)
    if path.suffix != ".shard":
        raise ArtifactValidationError(f"unsupported artifact file format: {value!r}")
    return path


def _validate_identifier(value: str, name: str) -> None:
    if not value or any(character in value for character in "/\\\x00"):
        raise ValueError(f"invalid {name}")


def _normalize_referenced_versions(versions: Mapping[str, int] | None) -> dict[str, int]:
    if versions is None:
        return {}
    if not isinstance(versions, Mapping):
        raise TypeError("referenced_versions must be a mapping")
    normalized: dict[str, int] = {}
    for name, version in versions.items():
        _validate_identifier(name, "referenced version name")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise ValueError("referenced versions must be positive integers")
        normalized[name] = version
    return normalized
