import json

import pytest
from search_engine.routing import QueryPlanner, RoutingMetadata, ShardRoutingMetadata
from search_engine.storage import ArtifactValidationError


def test_routing_metadata_round_trip_has_no_false_negative_for_inserted_terms() -> None:
    shard = ShardRoutingMetadata.from_terms("s1", "g1", 10, 100, ("python", "crawler", "index"))
    metadata = RoutingMetadata("g1", (shard,))
    restored = RoutingMetadata.from_bytes(metadata.to_bytes())
    assert all(restored.shards[0].bloom.may_contain(term) for term in ("python", "crawler", "index"))
    assert all(restored.shards[0].may_have_term(term) for term in ("python", "crawler", "index"))
    assert restored.shards[0].has_exact_term("crawler")
    assert not restored.shards[0].has_exact_term("zulu")
    assert not restored.shards[0].may_have_term("zulu")


def test_bloom_dimensions_scale_with_the_shard_vocabulary() -> None:
    shard = ShardRoutingMetadata.from_terms("large", "g1", 1000, 1000, (f"term{i}" for i in range(1000)))
    assert shard.bloom.bit_count >= 12000
    assert all(shard.may_have_term(f"term{i}") for i in range(1000))


def test_metadata_rejects_unsorted_terms_before_binary_lookup() -> None:
    shard = ShardRoutingMetadata.from_terms("s1", "g1", 1, 100, ("alpha", "zulu"))
    encoded = json.loads(RoutingMetadata("g1", (shard,)).to_bytes())
    encoded["shards"][0]["terms"] = ["zulu", "alpha"]
    with pytest.raises(ArtifactValidationError, match="routing metadata"):
        RoutingMetadata.from_bytes(json.dumps(encoded).encode())


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
    assert plan.stop_reason == "minimum_score"


def test_query_planner_stops_when_the_selected_shards_meet_confidence_target() -> None:
    high_value = ShardRoutingMetadata.from_terms("high", "g1", 1, 20, ("python", "crawler", "index"))
    lower_value = ShardRoutingMetadata.from_terms("low", "g1", 1, 20, ("python",))
    plan = QueryPlanner(
        RoutingMetadata("g1", (high_value, lower_value)), confidence_target=0.75
    ).plan("python crawler index")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["high"]
    assert plan.stopped_early
    assert plan.stop_reason == "confidence"


def test_query_planner_stops_before_a_shard_below_marginal_gain() -> None:
    high_value = ShardRoutingMetadata.from_terms("high", "g1", 1, 20, ("python", "crawler"))
    lower_value = ShardRoutingMetadata.from_terms("low", "g1", 1, 20, ("python",))
    plan = QueryPlanner(
        RoutingMetadata("g1", (high_value, lower_value)), minimum_marginal_gain=3.0
    ).plan("python crawler")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["high"]
    assert plan.stop_reason == "marginal_gain"


def test_query_planner_stops_when_remaining_score_cannot_justify_another_open() -> None:
    high_value = ShardRoutingMetadata.from_terms("high", "g1", 1, 20, ("python", "crawler"))
    lower_value = ShardRoutingMetadata.from_terms("low", "g1", 1, 20, ("python",))
    plan = QueryPlanner(
        RoutingMetadata("g1", (high_value, lower_value)), maximum_remaining_score=2.0
    ).plan("python crawler")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["high"]
    assert plan.stop_reason == "remaining_score"


def test_query_planner_returns_safe_partial_plan_when_byte_budget_is_exhausted() -> None:
    first = ShardRoutingMetadata.from_terms("first", "g1", 1, 80, ("python", "crawler"))
    second = ShardRoutingMetadata.from_terms("second", "g1", 1, 80, ("python",))
    plan = QueryPlanner(RoutingMetadata("g1", (first, second)), max_shards=2, byte_budget=80).plan("python crawler")
    assert [shard.shard_id for shard in plan.candidate_shards] == ["first"]
    assert not plan.stopped_early
    assert plan.budget_exhausted


def test_query_planner_safely_returns_no_shards_when_every_candidate_exceeds_budget() -> None:
    shard = ShardRoutingMetadata.from_terms("oversized", "g1", 1, 81, ("python",))
    plan = QueryPlanner(RoutingMetadata("g1", (shard,)), byte_budget=80).plan("python")
    assert plan.candidate_shards == ()
    assert plan.budget_exhausted


def test_query_planner_carries_normalized_analysis_for_explainable_intent() -> None:
    shard = ShardRoutingMetadata.from_terms("first", "g1", 1, 10, ("python", "crawler"))
    plan = QueryPlanner(RoutingMetadata("g1", (shard,))).plan('site:example.com "Python crawler"')
    assert plan.analysis is not None
    assert plan.analysis.domain_filter == "example.com"
    assert plan.analysis.phrases == ("python crawler",)


def test_query_planner_enforces_estimated_latency_budget() -> None:
    first = ShardRoutingMetadata.from_terms("first", "g1", 1, 10, ("python",), estimated_open_ms=4)
    second = ShardRoutingMetadata.from_terms("second", "g1", 1, 10, ("python",), estimated_open_ms=4)
    plan = QueryPlanner(RoutingMetadata("g1", (first, second)), latency_budget_ms=5).plan("python")
    assert len(plan.candidate_shards) == 1
    assert plan.estimated_latency_ms == 4
