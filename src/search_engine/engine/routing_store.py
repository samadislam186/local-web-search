"""Immutable, checksumed, memory-mappable routing metadata files."""

from __future__ import annotations

import mmap
import os
import tempfile
from pathlib import Path

from blake3 import blake3

from .routing import RoutingMetadata
from .storage import ArtifactValidationError


def write_routing_metadata(path: Path, metadata: RoutingMetadata) -> str:
    """Atomically write one immutable flat metadata file and return its checksum."""
    payload = metadata.to_bytes()
    checksum = blake3(payload).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        return checksum
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_routing_metadata(path: Path, *, expected_checksum: str | None = None) -> RoutingMetadata:
    """Read a flat metadata file through mmap and verify an optional expected checksum."""
    try:
        with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            payload = mapped[:]
    except (OSError, ValueError) as exc:
        raise ArtifactValidationError(f"cannot read routing metadata {path}: {exc}") from exc
    checksum = blake3(payload).hexdigest()
    if expected_checksum is not None and checksum != expected_checksum:
        raise ArtifactValidationError("routing metadata checksum mismatch")
    return RoutingMetadata.from_bytes(payload)
