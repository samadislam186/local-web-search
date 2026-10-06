"""Stable, collision-checked 128-bit URL identity."""

from __future__ import annotations

from dataclasses import dataclass

from blake3 import blake3

from .canonicalization import CanonicalUrl


@dataclass(frozen=True, slots=True, order=True)
class UrlId:
    """A BLAKE3-derived 128-bit identifier, represented as 16 raw bytes."""

    value: bytes

    def __post_init__(self) -> None:
        if len(self.value) != 16:
            raise ValueError("UrlId must contain exactly 128 bits")

    @classmethod
    def from_canonical_url(cls, url: CanonicalUrl) -> UrlId:
        return cls(blake3(url.value.encode("utf-8")).digest(length=16))

    @property
    def hex(self) -> str:
        return self.value.hex()
