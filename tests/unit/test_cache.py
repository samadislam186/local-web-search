from concurrent.futures import ThreadPoolExecutor

from search_engine.cache import BoundedShardCache, L1Cache


def test_l1_cache_tracks_hits_and_lru_eviction() -> None:
    cache: L1Cache[str, int] = L1Cache(max_entries=1)
    cache.put("a", 1)
    assert cache.get("a") == 1
    cache.put("b", 2)
    assert cache.get("a") is None
    assert (cache.hits, cache.misses) == (1, 1)


def test_l1_cache_expires_and_supports_explicit_invalidation() -> None:
    now = [10.0]
    cache: L1Cache[str, int] = L1Cache(max_entries=2, ttl_seconds=5, clock=lambda: now[0])
    cache.put("a", 1)
    now[0] += 5
    assert cache.get("a") is None
    cache.put("b", 2)
    assert cache.invalidate("b")
    assert not cache.invalidate("b")
    assert cache.snapshot().active_entries == 0


def test_l2_cache_respects_exact_byte_budget_and_rejects_cold_scan() -> None:
    cache: BoundedShardCache[str, str] = BoundedShardCache(max_entries=2, max_bytes=20)
    assert cache.get_or_load("hot", 10, lambda: "hot") == "hot"
    assert cache.get_or_load("hot", 10, lambda: "never") == "hot"
    assert cache.get_or_load("scan", 10, lambda: "scan") == "scan"
    # Cold candidate is rejected instead of evicting the more frequent hot shard.
    assert cache.get_or_load("cold", 10, lambda: "cold") == "cold"
    snapshot = cache.snapshot()
    assert snapshot.active_bytes <= 20
    assert snapshot.rejections == 1
    assert cache.get_or_load("hot", 10, lambda: "never") == "hot"


def test_l2_cache_never_exceeds_limits_under_concurrent_misses() -> None:
    cache: BoundedShardCache[str, str] = BoundedShardCache(max_entries=2, max_bytes=20)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: cache.get_or_load(value, 10, lambda: value), (str(i) for i in range(8))))
    assert results == [str(index) for index in range(8)]
    snapshot = cache.snapshot()
    assert snapshot.active_entries <= 2
    assert snapshot.active_bytes <= 20
