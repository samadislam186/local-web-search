"""Bounded L1 and frequency-aware L2 caches for serving-time data."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class L1Cache(Generic[K, V]):
    """Small TTL/LRU cache for tokenization and routing metadata results."""

    def __init__(self, max_entries: int = 1024, ttl_seconds: float = 60.0) -> None:
        if max_entries <= 0 or ttl_seconds <= 0:
            raise ValueError("max_entries and ttl_seconds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._values: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: K) -> V | None:
        value = self._values.pop(key, None)
        if value is None or value[0] <= monotonic():
            self.misses += 1
            return None
        self._values[key] = value
        self.hits += 1
        return value[1]

    def put(self, key: K, value: V) -> None:
        self._values.pop(key, None)
        self._values[key] = (monotonic() + self.ttl_seconds, value)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)


@dataclass(frozen=True, slots=True)
class CacheSnapshot:
    active_entries: int
    active_bytes: int
    hits: int
    misses: int
    rejections: int


class BoundedShardCache(Generic[K, V]):
    """L2 cache with a TinyLFU-inspired admission test and exact byte budget."""

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("max_entries and max_bytes must be positive")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._values: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._frequency: dict[K, int] = {}
        self._active_bytes = 0
        self.hits = 0
        self.misses = 0
        self.rejections = 0

    def get_or_load(self, key: K, size: int, loader: Callable[[], V]) -> V:
        if size <= 0 or size > self.max_bytes:
            raise ValueError("shard size must be positive and within byte budget")
        self._frequency[key] = self._frequency.get(key, 0) + 1
        resident = self._values.pop(key, None)
        if resident is not None:
            self._values[key] = resident
            self.hits += 1
            return resident[0]
        self.misses += 1
        value = loader()
        self._admit(key, value, size)
        return value

    def snapshot(self) -> CacheSnapshot:
        return CacheSnapshot(len(self._values), self._active_bytes, self.hits, self.misses, self.rejections)

    def _admit(self, key: K, value: V, size: int) -> None:
        while self._values and (len(self._values) >= self.max_entries or self._active_bytes + size > self.max_bytes):
            victim_key, victim = next(iter(self._values.items()))
            if self._frequency.get(key, 0) <= self._frequency.get(victim_key, 0):
                self.rejections += 1
                return
            self._values.pop(victim_key)
            self._active_bytes -= victim[1]
        if len(self._values) < self.max_entries and self._active_bytes + size <= self.max_bytes:
            self._values[key] = (value, size)
            self._active_bytes += size
        else:
            self.rejections += 1
