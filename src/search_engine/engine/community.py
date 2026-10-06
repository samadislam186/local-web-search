"""Signed, aggregate-only community contribution format for future releases."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .security import DetachedSignature, PinnedKeyVerifier
from .storage import ArtifactValidationError

CONTRIBUTION_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Contribution:
    contributor_id: str
    routing_hits: dict[str, int]
    ranking_deltas: dict[str, float]
    format_version: int = CONTRIBUTION_FORMAT_VERSION

    def canonical_bytes(self) -> bytes:
        _validate(self)
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sign(self, private_key: Ed25519PrivateKey, key_id: str) -> DetachedSignature:
        return DetachedSignature(key_id, _b64(private_key.sign(self.canonical_bytes())))


def verify_contribution(contribution: Contribution, signature: DetachedSignature, verifier: PinnedKeyVerifier) -> None:
    verifier.verify(contribution.canonical_bytes(), signature)


def contribution_to_bytes(contribution: Contribution, signature: DetachedSignature) -> bytes:
    return json.dumps(
        {"contribution": asdict(contribution), "signature": asdict(signature)}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def contribution_from_bytes(payload: bytes) -> tuple[Contribution, DetachedSignature]:
    try:
        data = json.loads(payload)
        contribution = Contribution(**data["contribution"])
        signature = DetachedSignature(**data["signature"])
        contribution.canonical_bytes()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("invalid community contribution") from exc
    return contribution, signature


def _validate(contribution: Contribution) -> None:
    if not contribution.contributor_id or len(contribution.contributor_id) > 128:
        raise ValueError("invalid contributor_id")
    if contribution.format_version != CONTRIBUTION_FORMAT_VERSION:
        raise ValueError("unsupported contribution format")
    for query_class, count in contribution.routing_hits.items():
        if len(query_class) != 32 or count < 0:
            raise ValueError("routing hits must have salted 128-bit query-class keys and non-negative counts")
    for url_id, delta in contribution.ranking_deltas.items():
        if len(url_id) != 32 or not math.isfinite(delta) or not -0.1 <= delta <= 0.1:
            raise ValueError("ranking deltas must have UrlId keys and bounded finite values")


def _b64(value: bytes) -> str:
    import base64

    return base64.b64encode(value).decode("ascii")
