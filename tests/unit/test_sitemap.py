from pathlib import Path

import pytest
from search_engine.sitemap import SitemapIngestor, parse_sitemap
from search_engine.state import CandidateRepository, CrawlStateDatabase


def test_sitemap_urlset_is_canonicalized_and_ingested(tmp_path: Path) -> None:
    xml = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
    <url><loc>HTTP://example.com/a#fragment</loc></url><url><loc>https://example.com/a</loc></url></urlset>"""
    database = CrawlStateDatabase(tmp_path / "crawl.sqlite3")
    database.initialize()
    parsed, ids = SitemapIngestor(CandidateRepository(database)).ingest(
        xml, sitemap_url="https://example.com/sitemap.xml"
    )
    assert parsed.page_urls == ("http://example.com/a", "https://example.com/a")
    assert len(ids) == 2
    with database.connection() as connection:
        assert connection.execute("SELECT source_record_id FROM candidate_provenance WHERE source='sitemap'").fetchone()[0] == "https://example.com/sitemap.xml"


def test_sitemap_index_and_unsafe_xml_are_handled() -> None:
    index = parse_sitemap(b"<sitemapindex><sitemap><loc>https://example.com/child.xml</loc></sitemap></sitemapindex>")
    assert index.child_sitemaps == ("https://example.com/child.xml",)
    with pytest.raises(ValueError, match="DTD"):
        parse_sitemap(b"<!DOCTYPE urlset><urlset></urlset>")
