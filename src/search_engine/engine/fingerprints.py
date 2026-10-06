"""Static-profile language hints and 64-bit SimHash document fingerprints."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from blake3 import blake3

_WORD = re.compile(r"[\w']+", re.UNICODE)

# Compact, inspectable seed profiles. These are data, not executable model artifacts.
_LANGUAGE_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "with", "that", "for", "from", "this", "search"}),
    "es": frozenset({"el", "la", "de", "que", "para", "con", "una", "los"}),
    "fr": frozenset({"le", "la", "de", "et", "les", "des", "pour", "une"}),
    "de": frozenset({"der", "die", "und", "das", "mit", "für", "ein", "ist"}),
}


@dataclass(frozen=True, slots=True)
class LanguageGuess:
    language: str | None
    confidence: float


def detect_language(text: str) -> LanguageGuess:
    tokens = Counter(token.lower() for token in _WORD.findall(text))
    if not tokens:
        return LanguageGuess(None, 0.0)
    scores = {language: sum(tokens[marker] for marker in markers) for language, markers in _LANGUAGE_MARKERS.items()}
    language, score = max(scores.items(), key=lambda item: item[1])
    return LanguageGuess(language if score else None, score / max(1, sum(tokens.values())))


def simhash64(text: str) -> int:
    """Compute stable 64-bit SimHash over normalized token frequencies."""
    frequencies = Counter(token.lower() for token in _WORD.findall(text))
    vector = [0] * 64
    for token, count in frequencies.items():
        hashed = int.from_bytes(blake3(token.encode("utf-8")).digest(length=8), "big")
        for bit in range(64):
            vector[bit] += count if hashed & (1 << bit) else -count
    return sum(1 << bit for bit, weight in enumerate(vector) if weight >= 0)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()
