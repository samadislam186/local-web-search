from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import Bm25fIndex, SearchDocument


def _document(url: str, title: str, body: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, title, body)


def test_bm25f_retrieves_from_postings_and_title_is_weighted() -> None:
    title_match = _document("https://example.com/title", "Python crawler", "ordinary text")
    body_match = _document("https://example.com/body", "ordinary text", "python crawler python crawler")
    index = Bm25fIndex((title_match, body_match))
    hits = index.search("python crawler", 10)
    assert {hit.url_id for hit in hits} == {title_match.url_id, body_match.url_id}
    assert hits[0].url_id == title_match.url_id


def test_bm25f_payload_round_trip_and_top_k_bound() -> None:
    document = _document("https://example.com/", "Search engine", "local lexical retrieval")
    index = Bm25fIndex((document,))
    restored = Bm25fIndex.from_payload(index.to_payload())
    assert restored.search("search", 1)[0].url == "https://example.com/"
    assert restored.search("missing", 1) == ()
