"""Deterministic, query-aware snippets from already normalized document text."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[\w]{2,}", re.UNICODE)


def make_snippet(text: str, query: str, *, max_length: int = 240) -> str:
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    normalized = " ".join(text.split())
    if len(normalized) <= max_length:
        return normalized
    terms = [re.escape(match.group(0)) for match in _TOKEN.finditer(query)]
    match = re.search("|".join(terms), normalized, re.IGNORECASE) if terms else None
    start = max(0, (match.start() if match else 0) - max_length // 3)
    end = min(len(normalized), start + max_length)
    start = normalized.rfind(" ", 0, start) + 1 if start else 0
    end = normalized.rfind(" ", 0, end) or end
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end]}{suffix}"
