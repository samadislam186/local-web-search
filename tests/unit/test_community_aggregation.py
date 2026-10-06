import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from search_engine.engine.community import Contribution
from search_engine.engine.community_aggregation import aggregate_contributions
from search_engine.engine.security import PinnedKeyVerifier


def test_community_aggregation_caps_sources_and_uses_median_delta() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    verifier = PinnedKeyVerifier({"key": base64.b64encode(public).decode("ascii")})
    url_id, query_class = "a" * 32, "b" * 32
    contributions = [
        Contribution("one", {query_class: 100_000}, {url_id: 0.1}),
        Contribution("two", {query_class: 2}, {url_id: 0.01}),
        Contribution("three", {query_class: 3}, {url_id: 0.02}),
    ]
    aggregate = aggregate_contributions([(item, item.sign(private, "key")) for item in contributions], verifier, per_source_routing_cap=10)
    assert aggregate.routing_hits[query_class] == 15
    assert aggregate.ranking_deltas[url_id] == 0.02
