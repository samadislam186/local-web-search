import pytest
from search_engine.routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata


def test_routing_metadata_round_trip_has_no_false_negative_for_inserted_terms() -> None:
    shard = ShardRoutingMetadata.from_terms("s1", "g1", 10, 100, ("python", "crawler", "index"))
    metadata = RoutingMetadata("g1", (shard,))
    restored = RoutingMetadata.from_bytes(metadata.to_bytes())
    assert all(restored.shards[0].bloom.may_contain(term) for term in ("python", "crawler", "index"))


def test_query_planner_uses_language_terms_and_byte_budget() -> None:
    first = ShardRoutingMetadata.from_terms("first", "g1", 10, 80, ("python", "crawler"), languages=("en",))
    second = ShardRoutingMetadata.from_terms("second", "g1", 10, 80, ("python",), languages=("en",))
    other_language = ShardRoutingMetadata.from_terms("spanish", "g1", 10, 20, ("python",), languages=("es",))
    plan = QueryPlanner(RoutingMetadata("g1", (first, second, other_language)), max_shards=2, byte_budget=100).plan("python crawler", language="en")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["first"]
    assert "term:crawler" in plan.candidate_shards[0].reasons


def test_routing_metadata_rejects_non_positive_shard_sizes() -> None:
    with pytest.raises(ValueError, match="byte_size"):
        ShardRoutingMetadata.from_terms("invalid", "g1", 1, -1, ("python",))


def test_query_planner_stops_before_low_value_remaining_shards() -> None:
    exact = ShardRoutingMetadata.from_terms("exact", "g1", 1, 20, ("python", "crawler"))
    marginal = ShardRoutingMetadata.from_terms("marginal", "g1", 1, 20, ("python",))
    planner = QueryPlanner(
        RoutingMetadata("g1", (exact, marginal)), max_shards=2, byte_budget=40, minimum_score=3.0
    )
    plan = planner.plan("python crawler")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["exact"]
    assert plan.stopped_early


def test_query_planner_returns_safe_partial_plan_when_byte_budget_is_exhausted() -> None:
    first = ShardRoutingMetadata.from_terms("first", "g1", 1, 80, ("python", "crawler"))
    second = ShardRoutingMetadata.from_terms("second", "g1", 1, 80, ("python",))
    plan = QueryPlanner(RoutingMetadata("g1", (first, second)), max_shards=2, byte_budget=80).plan("python crawler")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["first"]
    assert not plan.stopped_early
