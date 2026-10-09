"""Small deterministic cache-policy probes used before changing serving admission."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from .cache import BoundedShardCache


@dataclass(frozen=True, slots=True)
class CachePolicyResult:
    policy: str
    hits: int
    misses: int

    @property
    def hit_rate(self) -> float:
        return self.hits / max(1, self.hits + self.misses)


def compare_scan_resistance(workload: Iterable[str], *, capacity: int) -> tuple[CachePolicyResult, CachePolicyResult]:
    """Compare equal-sized TinyLFU-style and LRU caches for a reproducible trace."""
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    tiny_lfu: BoundedShardCache[str, str] = BoundedShardCache(max_entries=capacity, max_bytes=capacity)
    lru: OrderedDict[str, str] = OrderedDict()
    lru_hits = lru_misses = 0
    for key in workload:
        tiny_lfu.get_or_load(key, 1, lambda key=key: key)
        value = lru.pop(key, None)
        if value is not None:
            lru_hits += 1
            lru[key] = value
        else:
            lru_misses += 1
            if len(lru) == capacity:
                lru.popitem(last=False)
            lru[key] = key
    snapshot = tiny_lfu.snapshot()
    return (
        CachePolicyResult("tiny_lfu", snapshot.hits, snapshot.misses),
        CachePolicyResult("lru", lru_hits, lru_misses),
    )
