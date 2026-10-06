"""Bounded XML sitemap parsing and canonical candidate ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

from .canonicalization import canonicalize_url
from .errors import UrlCanonicalizationError
from .identity import UrlId
from .state import CandidateRepository

_MAX_SITEMAP_BYTES = 8 * 1024 * 1024
_MAX_LOCATIONS = 100_000


@dataclass(frozen=True, slots=True)
class SitemapParseResult:
    page_urls: tuple[str, ...]
    child_sitemaps: tuple[str, ...]
    rejected: int


def parse_sitemap(xml: bytes, *, max_bytes: int = _MAX_SITEMAP_BYTES, max_locations: int = _MAX_LOCATIONS) -> SitemapParseResult:
    if not 0 < len(xml) <= max_bytes:
        raise ValueError("sitemap exceeds byte limit or is empty")
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("DTD and entity declarations are not permitted in sitemap XML")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid sitemap XML") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        raise ValueError("unsupported sitemap root element")
    locations = [element.text.strip() for element in root.iter() if _local_name(element.tag) == "loc" and element.text]
    if len(locations) > max_locations:
        raise ValueError("sitemap location limit exceeded")
    canonical: list[str] = []
    rejected = 0
    for location in locations:
        try:
            canonical.append(canonicalize_url(location).value)
        except UrlCanonicalizationError:
            rejected += 1
    deduplicated = tuple(dict.fromkeys(canonical))
    if root_name == "sitemapindex":
        return SitemapParseResult((), deduplicated, rejected)
    return SitemapParseResult(deduplicated, (), rejected)


class SitemapIngestor:
    def __init__(self, candidates: CandidateRepository) -> None:
        self.candidates = candidates

    def ingest(self, xml: bytes, *, sitemap_url: str | None = None) -> tuple[SitemapParseResult, tuple[UrlId, ...]]:
        parsed = parse_sitemap(xml)
        source_record_id = canonicalize_url(sitemap_url).value if sitemap_url is not None else None
        url_ids = tuple(
            self.candidates.upsert(canonicalize_url(url), "sitemap", source_record_id=source_record_id)
            for url in parsed.page_urls
        )
        return parsed, url_ids


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()
