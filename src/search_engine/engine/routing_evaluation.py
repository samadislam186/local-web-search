"""Offline per-query routing acceptance against the actual all-shard serving pipeline."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .index_builder import _artifact_documents
from .manifest import ManifestStore
from .publisher import RetainedSegment, load_active_search_service, load_segment_catalog
from .query_analysis import analyze_query
from .routing import PlannedShard, QueryPlan, QueryPlanner, RoutingMetadata
from .security import PinnedKeyVerifier
from .service import SearchService


@dataclass(frozen=True, slots=True)
class RoutingQueryCase:
    case_id: str
    category: str
    query: str
    domain_filter: str | None = None
    language: str | None = None
    freshness: str | None = None
    expected_url_ids: tuple[str, ...] = ()
    allow_empty_oracle: bool = False


class _AllShardPlanner(QueryPlanner):
    def plan(self, query: str, **filters) -> QueryPlan:
        analysis = analyze_query(query)
        return QueryPlan(analysis.normalized,
                         tuple(PlannedShard(shard.shard_id, 1.0, ("offline_oracle",), shard.byte_size)
                               for shard in self.metadata.shards),
                         len(self.metadata.shards), sum(shard.byte_size for shard in self.metadata.shards), analysis=analysis)


def load_query_cases(path: Path, *, max_cases: int = 1000) -> tuple[RoutingQueryCase, ...]:
    cases = []
    with path.open(encoding="utf-8") as file:
        while line := file.readline(8193):
            if not line.strip():
                continue
            if len(line) > 8192 or len(cases) >= max_cases:
                raise ValueError("query corpus exceeds configured bounds")
            data = json.loads(line)
            data["expected_url_ids"] = tuple(data.get("expected_url_ids", ()))
            cases.append(RoutingQueryCase(**data))
    _validate_cases(tuple(cases))
    return tuple(cases)


def _validate_cases(cases: tuple[RoutingQueryCase, ...]) -> None:
    if not cases or len(cases) > 1000 or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("query cases must be non-empty, uniquely identified, and bounded")
    for case in cases:
        if not case.case_id or not case.category or not isinstance(case.query, str) or not 1 <= len(case.query) <= 500:
            raise ValueError("invalid routing query case")


def evaluate_published_routing(
    root: Path, cases: tuple[RoutingQueryCase, ...], *, shard_budgets: tuple[int, ...] = (1, 2, 5, 10),
    top_k: int = 10, byte_budget: int = 1 << 30, minimum_recall: float = 0.95,
    require_signatures: bool = True, verifier: PinnedKeyVerifier | None = None,
    scratch_directory: Path | None = None, now: datetime | None = None,
) -> dict:
    """Offline only: oracle opens all shards; normal planners remain budget-bound.

    Oracle means the same backend/local-top-K/ranker/filter contract across all
    shards, not human relevance or a new monolithic index with different BM25 IDF.
    """
    _validate_cases(cases)
    if not shard_budgets or any(not 1 <= budget <= 100 for budget in shard_budgets):
        raise ValueError("shard budgets must be within 1-100")
    if not 1 <= top_k <= 100 or byte_budget <= 0 or not 0 < minimum_recall <= 1:
        raise ValueError("invalid evaluation limits")
    checked_at = now or datetime.now(UTC)
    base = load_active_search_service(root, require_signatures=require_signatures, verifier=verifier)
    store = ManifestStore(root, require_signatures=require_signatures, verifier=verifier)
    manifest = store.active_manifest()
    indexes = [item for item in manifest.artifacts if item.header.shard_type in {"lexical_index", "tantivy_index"}]
    if {item.header.shard_id for item in indexes} != {shard.shard_id for shard in base.planner.metadata.shards}:
        raise ValueError("routing metadata does not cover exactly the published indexes")
    catalogs = {item.header.shard_id: item for item in manifest.artifacts if item.header.shard_type == "index_catalog"}
    directory = store.generations / manifest.generation
    oracle = SearchService(_AllShardPlanner(base.planner.metadata), base.indexes, ranker=base.ranker, signals=base.signals)
    records = []
    with TemporaryDirectory(prefix="routing-oracle-", dir=scratch_directory) as temporary, closing(sqlite3.connect(Path(temporary) / "ids.sqlite3")) as mapping:
        mapping.execute("PRAGMA cache_size=-8192")
        mapping.execute("PRAGMA temp_store=FILE")
        mapping.execute("CREATE TABLE documents(id TEXT PRIMARY KEY, shard TEXT)")
        for index in indexes:
            if index.header.shard_id in catalogs:
                _summary, identifiers, _version = load_segment_catalog(RetainedSegment(directory, index, catalogs[index.header.shard_id]))
            else:
                identifiers = (document.url_id.hex for document in _artifact_documents(directory, index))
            mapping.executemany("INSERT INTO documents VALUES (?, ?)", ((identifier, index.header.shard_id) for identifier in identifiers))
        mapping.commit()
        legacy = RoutingMetadata(base.planner.metadata.generation,
                                 tuple(replace(shard, summary_version=1, hosts=(), title_terms=(), title_ngrams=(), term_document_frequencies={}, term_min_url_ids={})
                                       for shard in base.planner.metadata.shards))
        for case in cases:
            arguments = {"top_k": top_k, "domain_filter": case.domain_filter, "language": case.language,
                         "freshness": case.freshness, "now": checked_at}
            try:
                expected = oracle.search(case.query, **arguments)
                oracle_ids = tuple(item.hit.url_id.hex for item in expected.results)
            except ValueError as exc:
                records.append({"case_id": case.case_id, "category": case.category, "query": case.query,
                                "error": str(exc), "policy": "oracle", "passed": False})
                continue
            source_shards = [mapping.execute("SELECT shard FROM documents WHERE id=?", (identifier,)).fetchone()[0] for identifier in oracle_ids]
            expectation_ok = (bool(oracle_ids) or case.allow_empty_oracle) and set(case.expected_url_ids).issubset(oracle_ids)
            for policy, metadata in (("presence_only", legacy), ("profiled", base.planner.metadata)):
                for budget in shard_budgets:
                    service = SearchService(QueryPlanner(metadata, max_shards=budget, byte_budget=byte_budget),
                                            base.indexes, ranker=base.ranker, signals=base.signals)
                    actual = service.search(case.query, **arguments)
                    selected = {item.shard_id for item in actual.plan.candidate_shards}
                    actual_ids = tuple(item.hit.url_id.hex for item in actual.results)
                    containment = sum(shard in selected for shard in source_shards) / len(oracle_ids) if oracle_ids else 1.0
                    recall = len(set(actual_ids) & set(oracle_ids)) / len(oracle_ids) if oracle_ids else float(not actual_ids)
                    bytes_opened = sum(item.byte_size for item in actual.plan.candidate_shards)
                    records.append({"case_id": case.case_id, "category": case.category, "query": case.query,
                                    "policy": policy, "max_shards": budget, "selected_shards": sorted(selected),
                                    "oracle_ids": oracle_ids, "returned_ids": actual_ids, "oracle_nonempty": bool(oracle_ids),
                                    "containment": containment, "result_recall": recall, "bytes_opened": bytes_opened,
                                    "budget_exhausted": actual.plan.budget_exhausted, "elapsed_ms": actual.total_ms,
                                    "expectation_ok": expectation_ok,
                                    "passed": expectation_ok and containment >= minimum_recall and recall >= minimum_recall
                                    and len(selected) <= budget and bytes_opened <= byte_budget})
    aggregates = defaultdict(list)
    for record in records:
        if "max_shards" in record:
            aggregates[(record["policy"], record["max_shards"], record["category"])].append(record)
    summaries = [{"policy": policy, "max_shards": budget, "category": category, "queries": len(values),
                  "mean_containment": sum(value["containment"] for value in values) / len(values),
                  "mean_result_recall": sum(value["result_recall"] for value in values) / len(values),
                  "passed": all(value["passed"] for value in values)}
                 for (policy, budget, category), values in sorted(aggregates.items())]
    return {"report_version": 1, "generation": manifest.generation, "top_k": top_k, "byte_budget": byte_budget,
            "minimum_recall": minimum_recall, "query_count": len(cases), "checked_at": checked_at.isoformat(),
            "summaries": summaries, "queries": records,
            "decision_by_budget": {str(budget): "pass" if not any("error" in record for record in records)
                                   and all(record["passed"] for record in records if record.get("policy") == "profiled" and record.get("max_shards") == budget)
                                   else "revise" for budget in shard_budgets},
            "limitations": ["Oracle uses the real shard backend and same bounded local candidate/ranking/filter contract",
                            "Containment is not human relevance; query/corpus provenance must be assessed separately",
                            "Caches are shared/warm; elapsed_ms is diagnostic, not a controlled latency benchmark",
                            "Offline oracle alone may scan all shards; normal search never exceeds its configured budget"]}
