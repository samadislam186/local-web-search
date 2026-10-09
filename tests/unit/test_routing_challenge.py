import importlib.util
from pathlib import Path

import pytest

from search_engine.engine.routing_evaluation import RoutingQueryCase, evaluate_published_routing
from search_engine.engine.tantivy_adapter import TantivyIndex


def test_native_varied_query_challenge(tmp_path):
    pytest.importorskip("tantivy")
    path = Path(__file__).resolve().parents[2] / "scripts/routing_benchmark.py"
    specification = importlib.util.spec_from_file_location("routing_benchmark", path)
    benchmark = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(benchmark)
    root, cases = benchmark.build_fixture(tmp_path / "fixture")
    report = evaluate_published_routing(root, cases, shard_budgets=(1, 5, 10), top_k=3,
                                        require_signatures=False)
    assert report["query_count"] == 24
    assert report["decision_by_budget"] == {"1": "revise", "5": "pass", "10": "pass"}
    assert all(row["containment"] == row["result_recall"] == 1
               for row in report["queries"] if row["policy"] == "profiled" and row["max_shards"] == 5)
    rejected = evaluate_published_routing(root, (RoutingQueryCase("missing", "positive", "nonexistentterm"),),
                                          shard_budgets=(5,), require_signatures=False)
    assert rejected["decision_by_budget"] == {"5": "revise"}


def test_native_cleanup_retries_only_transient_windows_errors(monkeypatch):
    class Temporary:
        attempts = 0

        def cleanup(self):
            self.attempts += 1
            if self.attempts == 1:
                error = OSError("native handle still closing")
                error.winerror = 145
                raise error

    index = TantivyIndex(None)
    temporary = Temporary()
    index._temporary = temporary
    monkeypatch.setattr("search_engine.engine.tantivy_adapter.time.sleep", lambda _: None)
    index.close()
    assert temporary.attempts == 2
    assert index._temporary is None
