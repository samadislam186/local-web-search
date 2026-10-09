"""Publish bounded synthetic batches and record native serving scale checkpoints."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, process_time

from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.evaluation import JudgedQuery, benchmark_search, evaluate_search
from search_engine.engine.identity import UrlId
from search_engine.engine.index import SearchDocument
from search_engine.engine.publisher import load_active_search_service, publish_lexical_generation
from search_engine.engine.ranking import LinearRanker
from search_engine.engine.tantivy_adapter import tantivy_capability


def batches(count: int, batch_size: int):
    """Generate bounded batches; each topic is one shard to make oracle evidence explicit."""
    for start in range(0, count, batch_size):
        number = start // batch_size
        documents = []
        for ordinal in range(start, min(count, start + batch_size)):
            canonical = canonicalize_url(f"https://scale.example/page/{ordinal}")
            documents.append(SearchDocument(
                UrlId.from_canonical_url(canonical), canonical.value,
                f"topic{number} document", f"Synthetic Python crawler search topic{number} record{ordinal}",
                crawled_at="2026-10-09T00:00:00+00:00",
            ))
        yield f"s{number:06d}", tuple(documents)


def run(root: Path, count: int, batch_size: int, runs: int) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    started, cpu_started = perf_counter(), process_time()
    manifest = publish_lexical_generation(root, "scale-g1", batches(count, batch_size), backend="tantivy")
    build_ms = (perf_counter() - started) * 1000
    build_cpu_ms = (process_time() - cpu_started) * 1000
    service = load_active_search_service(root, require_signatures=False, max_active_shards=10)
    service.ranker = LinearRanker(max_per_domain=100)
    report = benchmark_search(service, "topic0", runs=runs)
    relevant = {
        UrlId.from_canonical_url(canonicalize_url(f"https://scale.example/page/{ordinal}")).hex: 1.0
        for ordinal in range(min(count, batch_size))
    }
    oracle = tuple(item.url_id.hex for item in service.indexes.get("s000000").search("topic0", 10))
    # All topic0 documents are in s000000; exact containment follows from this
    # constructed assignment. Relevance labels are synthetic topic membership.
    relevance = evaluate_search(
        service, (JudgedQuery("topic0", relevant, oracle),),
        document_shards=dict.fromkeys(relevant, "s000000"),
    )
    result = {
        "report_version": 1, "created_at": datetime.now(UTC).isoformat(),
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "tantivy": tantivy_capability().version, "processor": platform.processor(),
                        "logical_cpus": os.cpu_count()},
        "limits": {"max_active_shards": 10, "byte_budget": 1 << 30, "query_runs": runs},
        "corpus": {"kind": "synthetic", "documents": count, "batch_size": batch_size,
                   "sampling_rule": "ordinal URL; one unique topic per batch", "generation": manifest.generation},
        "build": {"wall_ms": build_ms, "cpu_ms": build_cpu_ms,
                  "artifact_bytes": sum(path.stat().st_size for path in root.rglob("*.shard"))},
        "search": asdict(report), "relevance_smoke": asdict(relevance),
        "limitations": ["Synthetic membership smoke test; not representative relevance or broad-query routing evidence",
                        "RSS/mmap measurements are endpoint snapshots, not peak build memory",
                        "Crawl ingestion and graph computation are not measured in this checkpoint"],
    }
    (root / "benchmark.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10000, 100000, 1000000, 10000000])
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    if min(*args.sizes, args.batch_size, args.runs) <= 0:
        parser.error("sizes, batch-size and runs must be positive")
    args.directory.mkdir(parents=True, exist_ok=False)
    for count in args.sizes:
        # Conservative temporary/native-package allowance; refuse a checkpoint
        # rather than exhausting the user's disk while building synthetic data.
        if shutil.disk_usage(args.directory).free < max(1 << 30, count * 2048):
            raise RuntimeError(f"insufficient free disk space for {count} documents")
        print(f"starting documents={count}", flush=True)
        result = run(args.directory / str(count), count, args.batch_size, args.runs)
        print(json.dumps({"documents": count, "build": result["build"], "search": result["search"]}), flush=True)


if __name__ == "__main__":
    main()
