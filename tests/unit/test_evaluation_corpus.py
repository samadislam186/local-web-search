from search_engine.canonicalization import canonicalize_url
from search_engine.evaluation_corpus import RelevanceJudgment, RepresentativeCorpus
from search_engine.identity import UrlId
from search_engine.index import SearchDocument


def test_representative_corpus_round_trips_with_provenance_and_judgments(tmp_path) -> None:
    canonical = canonicalize_url("https://example.com/python")
    document = SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, "Python", "crawler")
    corpus = RepresentativeCorpus(
        provenance="fixture:manual-v1",
        sampling_rule="one known relevant document per query",
        documents=(document,),
        queries=("python crawler",),
        judgments=(RelevanceJudgment("python crawler", document.url_id.hex, 3),),
    )
    path = tmp_path / "corpus.json"
    corpus.write(path)
    assert RepresentativeCorpus.from_bytes(path.read_bytes()) == corpus
