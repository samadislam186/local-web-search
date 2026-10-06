from search_engine.parser import parse_html


def test_html_parser_extracts_searchable_fields_without_script_text() -> None:
    document = parse_html(
        """<html lang='en'><head><title> A title </title><meta name='description' content=' A description '>
        <link rel='canonical' href='/canonical'></head><body><script>ignore me</script><h1>Main heading</h1>
        <main><p>Useful text.</p><a href='/guide'> Guide link </a></main></body></html>""",
        "https://example.com/page",
    )
    assert document.title == "A title"
    assert document.canonical_hint == "https://example.com/canonical"
    assert document.headings == ("Main heading",)
    assert "Useful text." in document.main_text
    assert "ignore me" not in document.main_text
    assert document.anchor_text == (("https://example.com/guide", "Guide link"),)


def test_html_parser_handles_malformed_markup_base_urls_and_body_text_fallback() -> None:
    document = parse_html(
        "<html><head><base href='https://cdn.example/docs/'></head>"
        "<body>Loose searchable text<a href='guide'>Read guide</a>",
        "https://example.com/page",
    )
    assert document.main_text == "Loose searchable text Read guide"
    assert document.outgoing_links == ("https://cdn.example/docs/guide",)
    assert document.anchor_text == (("https://cdn.example/docs/guide", "Read guide"),)
