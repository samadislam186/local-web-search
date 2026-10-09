"""Deterministic query normalization and lightweight intent extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)
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


def analyze_query(query: str) -> QueryAnalysis:
    """Normalize an MVP query while retaining explicit filters for explainable plans."""
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    domain_filter = None
    freshness = None
    retained: list[str] = []
    for part in normalized.split():
        lowered = part.casefold()
        if lowered.startswith(("site:", "domain:")):
            candidate = part.split(":", 1)[1].casefold().strip("/")
            if not candidate:
                raise ValueError("domain filter must not be empty")
            domain_filter = candidate
        elif lowered.startswith("freshness:"):
            freshness = part.split(":", 1)[1].casefold()
        else:
            retained.append(part)
    searchable = " ".join(retained)
    phrases = tuple(" ".join(value.casefold().split()) for value in _PHRASE.findall(searchable) if value.strip())
    tokens = tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(searchable)))
    url_intent = bool(urlsplit(searchable).scheme and urlsplit(searchable).hostname) or any("." in token for token in tokens)
    fuzzy_terms = tuple(dict.fromkeys(match.group(1).casefold() for match in re.finditer(r"(?<!\w)([\w]{3,})~", searchable)))
    return QueryAnalysis(normalized, tokens, phrases, domain_filter, freshness, url_intent, fuzzy_terms)
