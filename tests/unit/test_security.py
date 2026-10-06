import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from search_engine.security import DetachedSignature, PinnedKeyVerifier, public_key_fingerprint
from search_engine.storage import ArtifactValidationError


def test_pinned_ed25519_signature_verification() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    payload = b"immutable release manifest"
    signature = DetachedSignature("release-1", base64.b64encode(private.sign(payload)).decode("ascii"))
    verifier = PinnedKeyVerifier({"release-1": base64.b64encode(public).decode("ascii")})
    verifier.verify(payload, signature)
    with pytest.raises(ArtifactValidationError):
        verifier.verify(b"modified", signature)
    with pytest.raises(ArtifactValidationError):
        PinnedKeyVerifier({"other": base64.b64encode(public).decode("ascii")}).verify(payload, signature)


def test_revoked_pinned_key_cannot_verify_even_with_a_valid_signature() -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")
    signature = DetachedSignature("release-1", base64.b64encode(private.sign(b"release")).decode("ascii"))
    verifier = PinnedKeyVerifier({"release-1": public}, revoked_key_ids=frozenset({"release-1"}))
    with pytest.raises(ArtifactValidationError, match="revoked"):
        verifier.verify(b"release", signature)


def test_third_party_keys_require_an_explicit_matching_fingerprint() -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")
    with pytest.raises(ValueError, match="requires explicit"):
        PinnedKeyVerifier.for_third_party({"third-party": public}, allowed_fingerprints=frozenset())
    with pytest.raises(ValueError, match="not explicitly allowed"):
        PinnedKeyVerifier.for_third_party(
            {"third-party": public}, allowed_fingerprints=frozenset({"0" * 64})
        )
    verifier = PinnedKeyVerifier.for_third_party(
        {"third-party": public}, allowed_fingerprints=frozenset({public_key_fingerprint(public)})
    )
    signature = DetachedSignature("third-party", base64.b64encode(private.sign(b"release")).decode("ascii"))
    verifier.verify(b"release", signature)
