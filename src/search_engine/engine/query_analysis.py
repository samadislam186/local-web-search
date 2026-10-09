"""Deterministic query normalization and lightweight intent extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .query_terms import TOKEN, validate_fuzzy_term

_TOKEN = TOKEN
_PHRASE = re.compile(r'"([^"\n]+)"')


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    normalized: str
    tokens: tuple[str, ...]
    phrases: tuple[str, ...]
    domain_filter: str | None
    freshness: str | None
    url_intent: bool
    fuzzy_terms: tuple[str, ...]
    navigation_host: str | None = None
    searchable: str = ""


def analyze_query(query: str) -> QueryAnalysis:
    """Normalize an MVP query while retaining explicit filters for explainable plans."""
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    domain_filter = None
    freshness = None
    retained: list[str] = []
    for part in re.findall(r'"[^"\n]*"|\S+', normalized):
        lowered = part.casefold()
        if lowered.startswith(("site:", "domain:")):
            domain_filter = normalize_domain(part.split(":", 1)[1])
        elif lowered.startswith("freshness:"):
            freshness = part.split(":", 1)[1].casefold()
        else:
            retained.append(part)
    searchable = " ".join(retained)
    phrases = tuple(" ".join(value.casefold().split()) for value in _PHRASE.findall(searchable) if value.strip())
    positive = []
    negate = False
    for part in retained:
        if part in {"AND", "OR"}:
            continue
        if part == "NOT":
            negate = True
            continue
        if negate or part.startswith("-"):
            negate = False
            continue
        positive.append(re.sub(r"^(title|body|headings|url|anchor|host):", "", part))
    tokens = tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(" ".join(positive))))
    navigation_host = None
    if len(retained) == 1 and not searchable.startswith('"'):
        try:
            parts = urlsplit(searchable if "://" in searchable else "//" + searchable)
            if parts.hostname and "." in parts.hostname and (not parts.scheme or parts.scheme in {"http", "https"}):
                navigation_host = parts.hostname.casefold().encode("idna").decode("ascii")
        except (ValueError, UnicodeError):
            pass
    url_intent = navigation_host is not None
    fuzzy_terms = tuple(dict.fromkeys(part[:-1].casefold() for part in retained if re.fullmatch(r"\w+~", part)))
    if len(fuzzy_terms) > 4:
        raise ValueError("at most four fuzzy terms are supported")
    for term in fuzzy_terms:
        validate_fuzzy_term(term)
    return QueryAnalysis(normalized, tokens, phrases, domain_filter, freshness, url_intent, fuzzy_terms, navigation_host, searchable)


def normalize_domain(value: str) -> str:
    candidate = value.casefold().strip("/").rstrip(".").strip("[]")
    if not candidate:
        raise ValueError("domain filter must not be empty")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid domain filter") from exc
