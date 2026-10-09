from search_engine.canonicalization import canonicalize_url
from search_engine.identity import UrlId
from search_engine.index import SearchDocument
from search_engine.routing_experiment import (
    assignment_balance_report,
    assignment_sweep_report,
    compare_assignment_sweeps,
    compare_assignments,
    domain_assignment,
    exhaustive_oracle,
    hash_assignment,
    run_experiment,
    select_routing_strategy,
    sweep_shard_budgets,
)


def _document(url: str, body: str) -> SearchDocument:
    canonical = canonicalize_url(url)
    return SearchDocument(UrlId.from_canonical_url(canonical), canonical.value, body, body)


def test_routing_experiment_measures_oracle_containment_under_budget(tmp_path) -> None:
    documents = (
        _document("https://python.example/a", "python crawler indexing"),
        _document("https://python.example/b", "python lexical search"),
        _document("https://other.example/a", "other subject"),
    )
    segments = domain_assignment(documents, 2)
    result = run_experiment("domain", segments, ("python crawler",), top_k=2, max_shards=1)
    assert result.query_count == 1
    assert 0 <= result.mean_top_k_containment <= 1
    assert result.mean_shards_opened <= 1
    assert result.p50_planning_ms >= 0
    assert hash_assignment(documents, 2)
    balance = assignment_balance_report(hash_assignment(documents, 2))
    assert balance.shard_count <= 2 and balance.max_documents >= balance.min_documents
    assert tuple(document.title for document in exhaustive_oracle(segments, "python crawler", 2))
    assert {result.assignment for result in compare_assignments(documents, ("python crawler",), shard_count=2, top_k=2, max_shards=1)} == {"hash", "domain", "hybrid"}
    sweep = sweep_shard_budgets("domain", segments, ("python crawler",), top_k=2, shard_budgets=(1, 2))
    assert sweep.assignment == "domain"
    assert [point.mean_shards_opened <= budget for point, budget in zip(sweep.points, (1, 2), strict=True)] == [True, True]
    assert sweep.points[1].mean_top_k_containment >= sweep.points[0].mean_top_k_containment
    report = sweep.to_dict((1, 2))
    assert report["assignment"] == "domain"
    assert report["budgets"][0]["max_shards"] == 1
    output = tmp_path / "routing-report.json"
    sweep.write_json(output, (1, 2))
    assert '"max_shards": 1' in output.read_text(encoding="utf-8")
    compared = compare_assignment_sweeps(documents, ("python crawler",), shard_count=2)
    comparison_report = assignment_sweep_report(compared, (1, 2, 5, 10))
    assert set(comparison_report["assignments"]) == {"domain", "hash", "hybrid"}
    assert comparison_report["shard_budgets"] == (1, 2, 5, 10)
    selected = select_routing_strategy(compared, (1, 2, 5, 10), minimum_containment=0.5)
    assert selected.outcome == "select" and selected.assignment is not None
    revise = select_routing_strategy(compared, (1, 2, 5, 10), minimum_containment=1.0)
    assert revise.outcome in {"select", "revise"}
