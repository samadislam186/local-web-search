from pathlib import Path

import pytest
from search_engine.authority import authority_from_payload, authority_to_payload
from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.publisher import load_active_search_service, publish_lexical_generation


def _document(url: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python", "python crawler")


def test_authority_payload_validates_url_ids_and_values() -> None:
    url_id = "a" * 32
    assert authority_from_payload(authority_to_payload({url_id: 0.75}))[url_id].authority == 0.75
    with pytest.raises(ValueError):
        authority_to_payload({"bad": 1.0})


def test_published_authority_signals_load_into_active_service(tmp_path: Path) -> None:
    document = _document("https://example.com/")
    publish_lexical_generation(
        tmp_path,
        "g1",
        {"s1": (document,)},
        authority_scores={document.url_id.hex: 0.75},
    )
    service = load_active_search_service(tmp_path, require_signatures=False)
    assert service.signals[document.url_id.hex].authority == 0.75
