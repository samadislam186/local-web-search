"""Pinned-key Ed25519 verification for externally supplied release artifacts."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .storage import ArtifactValidationError


@dataclass(frozen=True, slots=True)
class DetachedSignature:
    key_id: str
    signature_b64: str


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
