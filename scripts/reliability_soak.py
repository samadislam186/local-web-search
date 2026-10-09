"""Local synthetic crawl/serve/restart stress; no live-web traffic or production claims."""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

import psutil
from fastapi.testclient import TestClient

from search_engine.api import create_generation_app
from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.crawl_processing import CrawlResultProcessor
from search_engine.engine.crawl_store import CrawledDocumentRepository
from search_engine.engine.crawler_worker import CrawlerWorker
from search_engine.engine.fetch import FetchResult
from search_engine.engine.identity import UrlId
from search_engine.engine.index import SearchDocument
from search_engine.engine.manifest import ManifestStore
from search_engine.engine.publisher import publish_lexical_generation
from search_engine.engine.state import (
    CandidateRepository,
    CrawlStateDatabase,
    FrontierRepository,
    UrlState,
)
from search_engine.engine.storage import ArtifactValidationError


def crash_child(database_path: Path) -> None:
    """Commit a lease, then exit without closing an unrelated write transaction."""
    database = CrawlStateDatabase(database_path)
    now = datetime.now(UTC)
    lease = FrontierRepository(database).lease_next_details("crashed-child", now.isoformat(),
                                                           (now + timedelta(seconds=1)).isoformat())
    if lease is None:
        raise RuntimeError("crash fixture has no work")
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE candidates SET state_reason='uncommitted-crash' WHERE url_id=?", (lease.url_id.value,))
        os._exit(73)


def run(directory: Path, *, cycles: int = 10, requests_per_cycle: int = 100,
        threads: int = 4, duration_seconds: float = 0, backend: str = "tantivy",
        max_rss_growth_bytes: int = 64 << 20, max_handle_growth: int = 16) -> dict:
    if cycles < 2 or requests_per_cycle <= 0 or not 1 <= threads <= 32 or duration_seconds < 0:
        raise ValueError("invalid soak bounds")
    if max_rss_growth_bytes < 0 or max_handle_growth < 0:
        raise ValueError("resource growth limits must be nonnegative")
    directory.mkdir(parents=True, exist_ok=False)
    root = directory / "artifacts"
    segments = {}
    for number in range(8):
        url = canonicalize_url(f"https://fixture{number}.example/doc")
        segments[f"s{number}"] = (SearchDocument(UrlId.from_canonical_url(url), url.value,
                                                f"Topic{number}", f"Synthetic body number {number}"),)
    publish_lexical_generation(root, "good", segments, backend=backend)
    bad = publish_lexical_generation(root, "bad", segments, backend=backend)
    store = ManifestStore(root, require_signatures=False)
    store.rollback("good")
    corrupt = root / "generations" / "bad" / bad.artifacts[0].relative_path
    corrupt.write_bytes(b"injected corrupt fixture")
    rejected = False
    try:
        store.activate("bad")
    except ArtifactValidationError:
        rejected = True
    if not rejected or store.active_manifest().generation != "good":
        raise AssertionError("corrupt publication replaced a healthy generation")

    database_path = directory / "crawl.sqlite3"
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    url = canonicalize_url("https://crawl.example/document")
    identifier = candidates.upsert(url, "synthetic")
    candidates.transition(identifier, UrlState.LIVENESS_PENDING)
    candidates.transition(identifier, UrlState.LIVE)
    frontier = FrontierRepository(database)
    frontier.enqueue(identifier, "crawl.example", 1, datetime.now(UTC).isoformat())
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash-child", str(database_path)],
                           check=False, timeout=30, capture_output=True)
    if child.returncode != 73:
        raise AssertionError(f"crash child failed unexpectedly: {child.stderr.decode(errors='replace')}")
    with database.connection() as connection:
        expires = connection.execute("SELECT lease_expires_at FROM frontier").fetchone()[0]
        if connection.execute("SELECT state_reason FROM candidates").fetchone()[0] == "uncommitted-crash":
            raise AssertionError("uncommitted transaction survived process death")
    clock = datetime.fromisoformat(expires) + timedelta(seconds=1)

    class Probe:
        failed = False

        def probe(self, _url):
            if not self.failed:
                self.failed = True
                return FetchResult(503, {}, b"")
            return FetchResult(200, {"content-type": "text/html"}, b"<title>Durable crawl</title><p>Recovered content</p>")

    process = psutil.Process()
    probe = Probe()
    samples = []
    started = monotonic()
    cycle = requests = 0
    while cycle < cycles or monotonic() - started < duration_seconds:
        # Recreate repository and server instances to exercise persisted restart state.
        database = CrawlStateDatabase(database_path)
        candidates = CandidateRepository(database)
        frontier = FrontierRepository(database)
        documents = CrawledDocumentRepository(database)
        if cycle:
            frontier.enqueue(identifier, "crawl.example", 1, clock.isoformat())
        worker = CrawlerWorker(frontier, candidates, probe, CrawlResultProcessor(candidates), document_store=documents)
        arguments = {"now": clock.isoformat(), "lease_expires_at": (clock + timedelta(minutes=2)).isoformat(),
                     "retry_at": (clock + timedelta(seconds=1)).isoformat(), "next_allowed_at": clock.isoformat()}
        outcome = worker.run_once("soak-worker", **arguments)
        if not outcome.completed:
            clock += timedelta(seconds=2)
            arguments.update(now=clock.isoformat(), next_allowed_at=clock.isoformat())
            outcome = worker.run_once("restarted-worker", **arguments)
        if not outcome.completed or not documents.pending_for_index(limit=1):
            raise AssertionError("crawl retry/restart did not persist its document")
        clock += timedelta(seconds=3)
        app, service = create_generation_app(root, require_signatures=False, max_active_shards=2)
        with TestClient(app) as client:
            def request(number):
                response = client.post("/search", json={"query": f"topic{number % 8}"})
                if response.status_code != 200 or len(response.json()["results"]) != 1:
                    raise AssertionError(f"search failure: {response.status_code}")
            with ThreadPoolExecutor(max_workers=threads) as pool:
                for _ in pool.map(request, range(requests_per_cycle)):
                    requests += 1
            if service.stats()["loaded_shards"] > 2:
                raise AssertionError("cache exceeded configured shard count")
        if service.stats()["loaded_shards"]:
            raise AssertionError("server shutdown retained cache readers")
        del client, app, service, worker
        gc.collect()
        with database.connection() as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AssertionError("mutable database failed integrity check")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise AssertionError("mutable database has foreign-key violations")
            if connection.execute("SELECT COALESCE(SUM(active_leases), 0) FROM hosts").fetchone()[0]:
                raise AssertionError("host lease count did not recover")
        sample = {"cycle": cycle, "rss_bytes": process.memory_info().rss,
                  "handles": process.num_handles() if os.name == "nt" else process.num_fds()}
        # Keep a bounded tail even for overnight runs; never retain every request.
        samples.append(sample)
        if len(samples) > 256:
            samples.pop(0)
        if cycle == 0:
            baseline = sample
        cycle += 1
    rss_growth = samples[-1]["rss_bytes"] - baseline["rss_bytes"]
    handle_growth = samples[-1]["handles"] - baseline["handles"]
    return {"kind": "synthetic_reliability_soak", "backend": backend, "cycles": cycle, "requests": requests,
            "elapsed_seconds": monotonic() - started, "samples": samples, "rss_growth_bytes": rss_growth,
            "handle_growth": handle_growth,
            "passed": rss_growth <= max_rss_growth_bytes and handle_growth <= max_handle_growth,
            "limits": {"rss_growth_bytes": max_rss_growth_bytes, "handle_growth": max_handle_growth},
            "drills": ["abrupt worker exit", "uncommitted SQLite write rollback", "expired host lease reclaim",
                       "503 retry after repository restart", "concurrent API queries and cache churn",
                       "serving shutdown/restart", "corrupt generation refusal", "validated rollback"],
            "limitations": ["Synthetic corpus and in-process ASGI client, not live-network production traffic",
                            "Crawling uses a deterministic injected HTTP probe; no external sites are contacted",
                            "First-cycle warmup baseline and end-growth limits are diagnostics, not proof of leak freedom",
                            "Physical power loss, real disk exhaustion, external supervisors and multi-day deployments remain unverified"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crash-child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--requests-per-cycle", type=int, default=100)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--duration-seconds", type=float, default=0)
    parser.add_argument("--backend", choices=("reference", "tantivy"), default="tantivy")
    args = parser.parse_args()
    if args.crash_child:
        crash_child(args.crash_child)
        return
    if args.directory is None:
        parser.error("--directory is required and must not exist")
    report = run(args.directory, cycles=args.cycles, requests_per_cycle=args.requests_per_cycle,
                 threads=args.threads, duration_seconds=args.duration_seconds, backend=args.backend)
    (args.directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
