from search_engine.engine.snippets import make_snippet


def test_snippet_centers_query_term_and_respects_limit() -> None:
    text = "prefix " * 80 + "python crawler appears here " + "suffix " * 80
    snippet = make_snippet(text, "python crawler", max_length=80)
    assert "python crawler" in snippet
    assert len(snippet) <= 82
