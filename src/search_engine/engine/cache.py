"""Bounded L1 and frequency-aware L2 caches for serving-time data."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class L1Cache(Generic[K, V]):
    """Small TTL/LRU cache for tokenization and routing metadata results."""

    def __init__(
        self, max_entries: int = 1024, ttl_seconds: float = 60.0, *, clock: Callable[[], float] = monotonic
    ) -> None:
        if max_entries <= 0 or ttl_seconds <= 0:
            raise ValueError("max_entries and ttl_seconds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._values: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: K) -> V | None:
        value = self._values.pop(key, None)
        if value is None or value[0] <= self._clock():
            self.misses += 1
            return None
        self._values[key] = value
        self.hits += 1
        return value[1]

    def put(self, key: K, value: V) -> None:
        self._values.pop(key, None)
        self._values[key] = (self._clock() + self.ttl_seconds, value)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    def invalidate(self, key: K) -> bool:
        """Drop one value, for example after a generation activation."""
        return self._values.pop(key, None) is not None

    def clear(self) -> None:
        self._values.clear()

    def snapshot(self) -> CacheSnapshot:
        self._discard_expired()
        return CacheSnapshot(len(self._values), 0, self.hits, self.misses, 0)

    def _discard_expired(self) -> None:
        now = self._clock()
        for key, (expires_at, _value) in tuple(self._values.items()):
            if expires_at <= now:
                del self._values[key]


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
        self._frequency: OrderedDict[K, int] = OrderedDict()
        self.max_frequency_entries = max(1024, max_entries * 8)
        self._frequency_samples = 0
        self._active_bytes = 0
        self._lock = RLock()
        self.hits = 0
        self.misses = 0
        self.rejections = 0

    def get_or_load(self, key: K, size: int, loader: Callable[[], V]) -> V:
        if size <= 0 or size > self.max_bytes:
            raise ValueError("shard size must be positive and within byte budget")
        # Serialize miss/admission accounting so simultaneous requests cannot
        # over-admit beyond either configured capacity bound.
        with self._lock:
            self._frequency_samples += 1
            if self._frequency_samples >= self.max_frequency_entries * 10:
                self._frequency = OrderedDict((item, count // 2) for item, count in self._frequency.items() if count > 1)
                self._frequency_samples = 0
            self._frequency[key] = min(self._frequency.pop(key, 0) + 1, 65535)
            while len(self._frequency) > self.max_frequency_entries:
                self._frequency.popitem(last=False)
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
        with self._lock:
            return CacheSnapshot(len(self._values), self._active_bytes, self.hits, self.misses, self.rejections)

    def clear(self) -> None:
        """Release cache-owned references without closing objects still in use."""
        with self._lock:
            self._values.clear()
            self._frequency.clear()
            self._frequency_samples = 0
            self._active_bytes = 0

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
