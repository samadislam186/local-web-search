"""Turn a safely fetched HTML body into normalized document and discovery records."""

from __future__ import annotations

from dataclasses import dataclass

from blake3 import blake3

from .canonicalization import canonicalize_url
from .errors import UrlCanonicalizationError
from .fingerprints import LanguageGuess, detect_language, simhash64
from .identity import UrlId
from .parser import ParsedDocument, parse_html
from .state import CandidateRepository, UrlState


@dataclass(frozen=True, slots=True)
class CrawledDocument:
    url_id: UrlId
    canonical_url: str
    title: str
    body: str
    headings: tuple[str, ...]
    anchor_text: tuple[tuple[str, str], ...]
    content_hash: str
    simhash: int
    language: LanguageGuess
    parsed: ParsedDocument
    discovered_url_ids: tuple[UrlId, ...]


class CrawlResultProcessor:
    """Keeps parsing, duplicate signals, and link discovery deterministic and inspectable."""

    def __init__(self, candidates: CandidateRepository) -> None:
        self.candidates = candidates

    def process_html(self, url_id: UrlId, html: str, *, redirect_target: str | None = None) -> CrawledDocument:
        canonical = self.candidates.canonical_url(url_id)
        parsed = parse_html(html, canonical.value)
        discovered: list[UrlId] = []
        if redirect_target is not None:
            self._discover(redirect_target, "redirect", canonical.value, discovered)
        for outgoing in parsed.outgoing_links:
            self._discover(outgoing, "crawled_link", canonical.value, discovered)
        current = self.candidates.get_state(url_id)
        if current is UrlState.LIVE:
            self.candidates.transition(url_id, UrlState.CRAWLABLE)
            current = UrlState.CRAWLABLE
        if current is UrlState.CRAWLABLE:
            self.candidates.transition(url_id, UrlState.CRAWLED)
        normalized_text = " ".join((parsed.title, " ".join(parsed.headings), parsed.main_text))
        return CrawledDocument(
            url_id=url_id,
            canonical_url=canonical.value,
            title=parsed.title,
            body=parsed.main_text,
            headings=parsed.headings,
            anchor_text=parsed.anchor_text,
            content_hash=blake3(normalized_text.encode("utf-8")).hexdigest(),
            simhash=simhash64(normalized_text),
            language=detect_language(normalized_text),
            parsed=parsed,
            discovered_url_ids=tuple(item for item in dict.fromkeys(discovered) if item != url_id),
        )

    def _discover(self, raw_url: str, source: str, source_record_id: str, discovered: list[UrlId]) -> None:
        try:
            discovered.append(
                self.candidates.upsert(canonicalize_url(raw_url), source, source_record_id=source_record_id)
            )
        except UrlCanonicalizationError:
            return
