"""Shared lexical tokenization and bounded explicit ASCII typo expansion."""

import re
import string

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def validate_fuzzy_term(term: str) -> None:
    if not 3 <= len(term) <= 64 or not term.isascii() or not term.isalnum():
        raise ValueError("fuzzy terms must be 3-64 ASCII letters/digits")


def one_edit_variants(term: str) -> tuple[str, ...]:
    validate_fuzzy_term(term)
    alphabet = string.ascii_lowercase + string.digits
    splits = [(term[:position], term[position:]) for position in range(len(term) + 1)]
    variants = {term}
    variants.update(left + right[1:] for left, right in splits if right)
    variants.update(left + right[1] + right[0] + right[2:] for left, right in splits if len(right) > 1)
    variants.update(left + character + right[1:] for left, right in splits if right for character in alphabet)
    variants.update(left + character + right for left, right in splits for character in alphabet)
    return tuple(sorted(variants))
