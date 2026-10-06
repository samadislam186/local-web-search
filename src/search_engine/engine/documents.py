"""Stable conversion from crawler output to indexable search documents."""

from __future__ import annotations

from .crawl_processing import CrawledDocument
from .index import SearchDocument


def search_document_from_crawl(document: CrawledDocument) -> SearchDocument:
    """Map parsed crawl fields to the lexical index contract without raw HTML retention."""
    return SearchDocument(
        url_id=document.url_id,
        url=document.canonical_url,
        title=document.title,
        body=document.body,
        headings=" ".join(document.headings),
        anchor=" ".join(text for _url, text in document.anchor_text),
    )
