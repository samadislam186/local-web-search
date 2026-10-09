from search_engine.cache_benchmark import compare_scan_resistance


def test_tiny_lfu_admission_resists_a_cold_scan_better_than_lru() -> None:
    # Repeated hot reads establish frequency, then a one-pass scan attempts eviction.
    workload = ("hot", "hot", "hot", "warm", "warm", "a", "b", "c", "d", "hot", "warm")
    tiny_lfu, lru = compare_scan_resistance(workload, capacity=2)
    assert tiny_lfu.hit_rate > lru.hit_rate
    assert tiny_lfu.hits == 5
    assert lru.hits == 3
