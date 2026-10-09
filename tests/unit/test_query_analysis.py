from search_engine.query_analysis import analyze_query


def test_query_analysis_extracts_phrase_url_domain_freshness_and_fuzzy_intent() -> None:
    analysis = analyze_query('site:example.com freshness:week "python crawler" pyhton~')
    assert analysis.domain_filter == "example.com"
    assert analysis.freshness == "week"
    assert analysis.phrases == ("python crawler",)
    assert analysis.tokens == ("python", "crawler", "pyhton")
    assert analysis.fuzzy_terms == ("pyhton",)
