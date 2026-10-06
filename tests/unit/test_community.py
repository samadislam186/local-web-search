import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from search_engine.engine.community import (
    Contribution,
    contribution_from_bytes,
    contribution_to_bytes,
    verify_contribution,
)
from search_engine.engine.security import PinnedKeyVerifier
from search_engine.engine.storage import ArtifactValidationError


def test_community_contribution_is_bounded_signed_and_round_trips() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = PinnedKeyVerifier({"contributor-key": base64.b64encode(public).decode("ascii")})
    contribution = Contribution("contributor-1", {"a" * 32: 10}, {"b" * 32: 0.05})
    signature = contribution.sign(private, "contributor-key")
    restored, restored_signature = contribution_from_bytes(contribution_to_bytes(contribution, signature))
    verify_contribution(restored, restored_signature, verifier)
    with pytest.raises(ValueError):
        Contribution("contributor-1", {}, {"b" * 32: 1.0}).canonical_bytes()
    with pytest.raises(ArtifactValidationError):
        verify_contribution(Contribution("other", {}, {}), signature, verifier)
