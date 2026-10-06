"""Deterministic, dependency-free HTML extraction for the first crawler slice."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str
    meta_description: str | None
    canonical_hint: str | None
    language_hint: str | None
    headings: tuple[str, ...]
    main_text: str
    outgoing_links: tuple[str, ...]
    anchor_text: tuple[tuple[str, str], ...]


class _ExtractionParser(HTMLParser):
    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
    _TEXT_TAGS = frozenset({"p", "li", "article", "main", "section", "div", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.base_url = page_url
        self.ignored_depth = 0
        self.text_depth = 0
        self.body_depth = 0
        self.title_parts: list[str] = []
        self.title_depth = 0
        self.headings: list[str] = []
        self._heading_parts: list[str] | None = None
        self.text_parts: list[str] = []
        self.fallback_parts: list[str] = []
        self.links: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._anchor_url: str | None = None
        self._anchor_parts: list[str] = []
        self.meta_description: str | None = None
        self.canonical_hint: str | None = None
        self.language_hint: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value for name, value in attrs}
        if tag in self._IGNORED_TAGS:
            self.ignored_depth += 1
            return
        if tag == "html" and attributes.get("lang"):
            self.language_hint = attributes["lang"]
        if tag == "base" and attributes.get("href"):
            self.base_url = urljoin(self.page_url, attributes["href"] or "")
        if tag == "meta" and (attributes.get("name") or "").lower() == "description":
            self.meta_description = _clean(attributes.get("content") or "") or None
        if tag == "link" and (attributes.get("rel") or "").lower() == "canonical" and attributes.get("href"):
            self.canonical_hint = urljoin(self.base_url, attributes["href"] or "")
        if tag == "title":
            self.title_depth += 1
        if tag == "body":
            self.body_depth += 1
        if tag in self._TEXT_TAGS:
            self.text_depth += 1
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._heading_parts = []
        if tag == "a" and attributes.get("href"):
            self._anchor_url = urljoin(self.base_url, attributes["href"] or "")
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag == "body" and self.body_depth:
            self.body_depth -= 1
        if tag in self._TEXT_TAGS and self.text_depth:
            self.text_depth -= 1
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit() and self._heading_parts is not None:
            heading = _clean(" ".join(self._heading_parts))
            if heading:
                self.headings.append(heading)
            self._heading_parts = None
        if tag == "a" and self._anchor_url is not None:
            anchor = _clean(" ".join(self._anchor_parts))
            self.links.append(self._anchor_url)
            self.anchors.append((self._anchor_url, anchor))
            self._anchor_url = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        if self.title_depth:
            self.title_parts.append(data)
        if self.text_depth:
            self.text_parts.append(data)
        elif self.body_depth:
            self.fallback_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._anchor_url is not None:
            self._anchor_parts.append(data)


def parse_html(html: str, page_url: str) -> ParsedDocument:
    parser = _ExtractionParser(page_url)
    parser.feed(html)
    parser.close()
    return ParsedDocument(
        title=_clean(" ".join(parser.title_parts)),
        meta_description=parser.meta_description,
        canonical_hint=parser.canonical_hint,
        language_hint=parser.language_hint,
        headings=tuple(parser.headings),
        main_text=_clean(" ".join(parser.text_parts or parser.fallback_parts)),
        outgoing_links=tuple(dict.fromkeys(parser.links)),
        anchor_text=tuple(parser.anchors),
    )


def _clean(value: str) -> str:
    return " ".join(value.split())
