import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchHit
from search_engine.ranking import LinearRanker, RankingSignals


def _hit(value: str, score: float = 1.0) -> SearchHit:
    canonical = canonicalize_url(value)
    return SearchHit(UrlId.from_canonical_url(canonical), score, "Python", canonical.value, "Python crawler")


def test_ranker_exposes_the_versioned_feature_schema_and_contributions() -> None:
    hit = _hit("https://example.com/python")
    signals = RankingSignals(title_match=1.0, phrase_match=0.5, url_match=0.5, anchor_match=0.25)
    ranked = LinearRanker().rank((hit,), {hit.url_id.hex: signals}, 1)
    assert ranked[0].features == {"bm25": 1.0, **signals.values()}
    assert ranked[0].score == pytest.approx(1.375)
    assert sum(ranked[0].contributions.values()) == pytest.approx(ranked[0].score)


def test_ranker_applies_diversity_only_after_scoring_the_full_candidate_set() -> None:
    first = _hit("https://example.com/first", score=1.0)
    second = _hit("https://example.com/second", score=2.0)
    third = _hit("https://other.example/third", score=0.5)
    ranked = LinearRanker(max_per_domain=1).rank((first, second, third), {}, 2)
    assert [item.hit.url_id for item in ranked] == [second.url_id, third.url_id]


def test_ranker_rejects_unknown_or_non_finite_feature_schemas() -> None:
    assert RankingSignals(0.75).authority == 0.75
    with pytest.raises(ValueError, match="schema"):
        RankingSignals(schema_version=2)
    with pytest.raises(ValueError, match="finite"):
        RankingSignals(authority=float("nan"))
