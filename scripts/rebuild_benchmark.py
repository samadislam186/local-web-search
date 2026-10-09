"""Measure real builder RSS, scratch disk, IO, and unchanged-shard reuse.

An optional real crawl database is backed up read-only into a NEW benchmark
directory. Only that copy is modified. Without it, the corpus is synthetic.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, process_time

import psutil
from blake3 import blake3

from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.crawl_store import CrawledDocumentRepository
from search_engine.engine.identity import UrlId
from search_engine.engine.index_builder import LocalIndexBuilder
from search_engine.engine.publisher import load_active_search_service
from search_engine.engine.state import CandidateRepository, CrawlStateDatabase, UrlState
from search_engine.engine.tantivy_adapter import tantivy_capability


def _synthetic(database: CrawlStateDatabase, count: int, body_bytes: int) -> None:
    """Bounded import of synthetic normalized records, not a live-web crawl."""
    body = ("Python search crawler bounded offline indexing. " * (body_bytes // 47 + 1))[:body_bytes]
    timestamp = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        for start in range(0, count, 500):
            candidates, documents = [], []
            for number in range(start, min(count, start + 500)):
                url = canonicalize_url(f"https://rebuild.example/page/{number}")
                identifier = UrlId.from_canonical_url(url).value
                target = f"https://rebuild.example/page/{(number + 1) % count}"
                candidates.append((identifier, url.value, UrlState.CRAWLED, timestamp, timestamp))
                documents.append((identifier, f"Document {number}", body, json.dumps(((target, "next"),)),
                                  blake3(body.encode()).hexdigest(), timestamp))
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("""INSERT INTO candidates(url_id, canonical_url, canonicalization_version,
                state, source, created_at, updated_at) VALUES (?, ?, 1, ?, 'synthetic_benchmark', ?, ?)""", candidates)
            connection.executemany("""INSERT INTO crawled_documents(url_id, title, body, headings_json,
                anchor_json, content_hash, simhash_hex, language, crawled_at)
                VALUES (?, ?, ?, '[]', ?, ?, '0000000000000000', 'en', ?)""", documents)
            connection.commit()


def _scratch_bytes(root: Path, generation: str) -> int:
    total = 0
    paths = list(root.glob(".rebuild-*")) + [root / "generations" / generation]
    for directory in paths:
        for path in directory.rglob("*"):
            if not (directory.name.startswith(".rebuild-") or any(part.startswith(".") for part in path.relative_to(directory).parts)):
                continue
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def _measure(builder: LocalIndexBuilder, root: Path, generation: str, **limits) -> dict:
    process = psutil.Process()
    stopped = threading.Event()
    baseline_rss = process.memory_info().rss
    measurements = {"rss": baseline_rss, "scratch": 0}
    errors = []

    def sample():
        while not stopped.is_set():
            try:
                measurements["rss"] = max(measurements["rss"], process.memory_info().rss)
                measurements["scratch"] = max(measurements["scratch"], _scratch_bytes(root, generation))
            except (OSError, psutil.Error) as exc:
                errors.append(type(exc).__name__)
            stopped.wait(0.02)

    def io():
        try:
            return process.io_counters()
        except (OSError, psutil.Error):
            return None

    before_io = io()
    free_before = shutil.disk_usage(root).free
    old_files = {(path.stat().st_dev, path.stat().st_ino) for path in (root / "generations").rglob("*.shard")}
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started, cpu_started = perf_counter(), process_time()
    try:
        result = builder.build(root, generation, **limits)
    finally:
        wall_ms, cpu_ms = (perf_counter() - started) * 1000, (process_time() - cpu_started) * 1000
        stopped.set()
        thread.join()
    after_io = io()
    files = [path.stat() for path in (root / "generations" / generation).rglob("*.shard")]
    return {
        "generation": generation, "documents": result.document_count, "shards": result.shard_count,
        "reused_shards": result.reused_shards, "rebuilt_shards": result.rebuilt_shards,
        "wall_ms": wall_ms, "cpu_ms": cpu_ms, "baseline_rss_bytes": baseline_rss,
        "sampled_peak_rss_bytes": measurements["rss"],
        "sampled_peak_scratch_bytes": measurements["scratch"], "sample_interval_ms": 20,
        "process_io_read_bytes": after_io.read_bytes - before_io.read_bytes if after_io and before_io else None,
        "process_io_write_bytes": after_io.write_bytes - before_io.write_bytes if after_io and before_io else None,
        "logical_artifact_bytes": sum(item.st_size for item in files),
        "new_unique_artifact_bytes": sum(item.st_size for item in files if (item.st_dev, item.st_ino) not in old_files),
        "observed_free_disk_delta_bytes": free_before - shutil.disk_usage(root).free,
        "sampling_errors": sorted(set(errors)),
    }


def _mark_update(database: CrawlStateDatabase, updates: int) -> None:
    timestamp = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        rows = connection.execute("SELECT url_id, body FROM crawled_documents ORDER BY url_id LIMIT ?", (updates,)).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            body = str(row["body"])
            if "incrementalcheckpoint" not in body:
                body += " incrementalcheckpoint"
            connection.execute("""UPDATE crawled_documents SET title='Incremental checkpoint', body=?,
                content_hash=?, crawled_at=?, indexed_generation=NULL WHERE url_id=?""",
                (body, blake3(body.encode()).hexdigest(), timestamp, row["url_id"]))
            connection.execute("UPDATE candidates SET state=? WHERE url_id=?", (UrlState.CRAWLED, row["url_id"]))
        connection.commit()


def run(directory: Path, count: int, body_bytes: int, backend: str, updates: int, source: Path | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    database = CrawlStateDatabase(directory / "crawl.sqlite3")
    provenance = {"kind": "synthetic", "documents": count, "body_bytes": body_bytes,
                  "rule": "ordinal URLs, repeated normalized text, one cyclic outbound link per page"}
    if source is not None:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(database.path)) as writer:
            reader.backup(writer)
        digest = blake3()
        with database.path.open("rb") as file:
            while chunk := file.read(1 << 20):
                digest.update(chunk)
        provenance = {"kind": "provided_database_copy", "source": str(source.resolve()), "snapshot_blake3": digest.hexdigest(),
                      "note": "read-only online backup includes WAL; corpus representativeness is not inferred"}
    database.initialize()
    if source is None:
        _synthetic(database, count, body_bytes)
    with database.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM crawled_documents").fetchone()[0]
        if count < updates:
            raise ValueError("database has fewer crawl documents than the update count")
        connection.execute("UPDATE crawled_documents SET indexed_generation=NULL")
    root = directory / "artifacts"
    root.mkdir()
    builder = LocalIndexBuilder(CrawledDocumentRepository(database), CandidateRepository(database))
    limits = {"max_documents": count, "shard_count": 32, "segment_documents": 1000, "backend": backend}
    phases = []
    for generation, full in (("initial", False), ("incremental", False), ("full-comparison", True)):
        if generation != "initial":
            _mark_update(database, updates)
        print(f"starting phase={generation} documents={count}", flush=True)
        phase = _measure(builder, root, generation, **limits, full_rebuild=full)
        phases.append(phase)
        print(json.dumps(phase), flush=True)
    service = load_active_search_service(root, require_signatures=False)
    if not service.search("incrementalcheckpoint").results:
        raise RuntimeError("updated document was not searchable")
    result = {
        "report_version": 1, "created_at": datetime.now(UTC).isoformat(), "corpus": provenance,
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "tantivy": tantivy_capability().version, "logical_cpus": os.cpu_count()},
        "limits": limits, "updated_documents": updates, "phases": phases,
        "limitations": ["Sampled RSS/scratch peaks can miss sub-20ms spikes; sampler overhead is included",
                        "Process IO counters are not guaranteed physical disk IO; filesystem caches are not flushed",
                        "Full comparison follows incremental update in the same process; caches/allocators may be warm",
                        "Validation still streams retained checksums; global PageRank is recomputed",
                        "Synthetic data does not establish representative real-corpus acceptance"],
    }
    (directory / "rebuild-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new benchmark directory")
    parser.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--body-bytes", type=int, default=2048)
    parser.add_argument("--updates", type=int, default=1)
    parser.add_argument("--backend", choices=("reference", "tantivy"), default="tantivy")
    parser.add_argument("--database", type=Path, help="optional real crawl database; original is never modified")
    args = parser.parse_args()
    if min(*args.sizes, args.body_bytes, args.updates) <= 0:
        parser.error("sizes, body-bytes, and updates must be positive")
    if args.database is not None and not args.database.is_file():
        parser.error("database must be an existing file")
    args.directory.mkdir(parents=True, exist_ok=False)
    sizes = [0] if args.database else args.sizes
    for count in sizes:
        required = args.database.stat().st_size * 6 if args.database else count * (args.body_bytes * 6 + 4096)
        if shutil.disk_usage(args.directory).free < max(1 << 30, required):
            raise RuntimeError("insufficient free disk space for checkpoint")
        run(args.directory / ("real-copy" if args.database else str(count)), count, args.body_bytes,
            args.backend, args.updates, args.database)


if __name__ == "__main__":
    main()
