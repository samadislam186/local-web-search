"""Pinned-key Ed25519 verification for externally supplied release artifacts."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .storage import ArtifactValidationError


@dataclass(frozen=True, slots=True)
class DetachedSignature:
    key_id: str
    signature_b64: str


@dataclass(frozen=True, slots=True)
class ReleaseSigner:
    key_id: str
    private_key: Ed25519PrivateKey

    @classmethod
    def from_pem(cls, path: Path, key_id: str) -> ReleaseSigner:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey) or not key_id.strip():
            raise ValueError("release signing requires an Ed25519 PEM key and non-empty key ID")
        return cls(key_id, key)

    def sign(self, payload: bytes) -> DetachedSignature:
        return DetachedSignature(self.key_id, base64.b64encode(self.private_key.sign(payload)).decode("ascii"))

    def verifier(self) -> PinnedKeyVerifier:
        raw = self.private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return PinnedKeyVerifier({self.key_id: base64.b64encode(raw).decode("ascii")})


class PinnedKeyVerifier:
    """Verifies detached signatures only from roots pinned by the operator/software."""

    def __init__(self, roots: Mapping[str, str], *, revoked_key_ids: frozenset[str] = frozenset()) -> None:
        if not roots:
            raise ValueError("at least one pinned public key is required")
        if any(not key_id for key_id in roots):
            raise ValueError("pinned key IDs must not be empty")
        self._keys = {key_id: _decode_public_key(encoded) for key_id, encoded in roots.items()}
        self._revoked_key_ids = frozenset(revoked_key_ids)

    @classmethod
    def for_third_party(
        cls,
        roots: Mapping[str, str],
        *,
        allowed_fingerprints: frozenset[str],
        revoked_key_ids: frozenset[str] = frozenset(),
    ) -> PinnedKeyVerifier:
        """Create third-party trust only after every key matches an explicit fingerprint."""
        if not allowed_fingerprints:
            raise ValueError("third-party trust requires explicit allowed_fingerprints")
        configured = {public_key_fingerprint(encoded) for encoded in roots.values()}
        if not configured <= allowed_fingerprints:
            raise ValueError("third-party key fingerprint is not explicitly allowed")
        return cls(roots, revoked_key_ids=revoked_key_ids)

    def verify(self, payload: bytes, signature: DetachedSignature) -> None:
        if signature.key_id in self._revoked_key_ids:
            raise ArtifactValidationError(f"signature key is revoked: {signature.key_id}")
        key = self._keys.get(signature.key_id)
        if key is None:
            raise ArtifactValidationError(f"signature key is not pinned: {signature.key_id}")
        try:
            decoded_signature = base64.b64decode(signature.signature_b64, validate=True)
            key.verify(decoded_signature, payload)
        except (InvalidSignature, ValueError) as exc:
            raise ArtifactValidationError("invalid artifact signature") from exc


def _decode_public_key(encoded: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("wrong Ed25519 public key length")
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise ValueError("invalid pinned Ed25519 public key") from exc


def public_key_fingerprint(encoded: str) -> str:
    """Return the stable SHA-256 fingerprint used in third-party trust policy."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("invalid pinned Ed25519 public key") from exc
    if len(raw) != 32:
        raise ValueError("invalid pinned Ed25519 public key")
    return hashlib.sha256(raw).hexdigest()
