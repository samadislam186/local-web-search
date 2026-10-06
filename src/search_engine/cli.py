"""Minimal inspectable command line entry point."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .api import create_app
from .engine.canonicalization import canonicalize_url
from .engine.common_crawl.client import CommonCrawlIndexClient
from .engine.common_crawl.ingestion import CommonCrawlIngestor, IngestionStats
from .engine.crawl_processing import CrawlResultProcessor
from .engine.crawl_store import CrawledDocumentRepository
from .engine.crawler_runner import CrawlerRunner, CrawlRunStats
from .engine.crawler_worker import CrawlerWorker
from .engine.feedback import SqliteFeedbackAggregator
from .engine.fetch import SafeHttpProbe
from .engine.identity import UrlId
from .engine.index_builder import IndexBuildResult, LocalIndexBuilder
from .engine.liveness_runner import LivenessRunner, LivenessRunStats
from .engine.liveness_worker import LivenessWorker
from .engine.publisher import load_active_search_service
from .engine.robots_manager import RobotsManager
from .engine.security import PinnedKeyVerifier
from .engine.state import (
    CandidateRepository,
    CommonCrawlPlanRepository,
    CrawlStateDatabase,
    FrontierRepository,
    HostRepository,
    IngestionProgressRepository,
    LivenessRepository,
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    submitted: int
    created: int
    existing: int


def main() -> None:
    parser = argparse.ArgumentParser(prog="open-search")
    commands = parser.add_subparsers(dest="command", required=True)
    canonicalize = commands.add_parser("canonicalize")
    canonicalize.add_argument("url")
    initialize = commands.add_parser("init-state")
    initialize.add_argument("database", type=Path)
    inspect_state = commands.add_parser("inspect-state")
    inspect_state.add_argument("database", type=Path)
    backup_state = commands.add_parser("backup-state")
    backup_state.add_argument("database", type=Path)
    backup_state.add_argument("destination", type=Path)
    seed = commands.add_parser("seed")
    seed.add_argument("database", type=Path)
    seed.add_argument("urls", nargs="+")
    seed.add_argument("--source", default="manual_seed")
    common_crawl = commands.add_parser("ingest-common-crawl")
    common_crawl.add_argument("database", type=Path)
    common_crawl.add_argument("crawl_id")
    common_crawl.add_argument("url_pattern")
    common_crawl.add_argument("--partition", default="default")
    common_crawl.add_argument("--match-type", default="domain", choices=("domain", "prefix", "host", "exact"))
    list_common_crawl = commands.add_parser("list-common-crawl")
    list_common_crawl.add_argument("--limit", type=int, default=20)
    build_index = commands.add_parser("build-index")
    build_index.add_argument("database", type=Path, help="crawl-state SQLite database")
    build_index.add_argument("root", type=Path, help="local artifact root")
    build_index.add_argument("generation", help="new immutable generation identifier")
    build_index.add_argument("--max-documents", type=int, default=10_000)
    build_index.add_argument("--shard-count", type=int, default=1)
    build_index.add_argument("--target-shard-bytes", type=int)
    liveness_run = commands.add_parser("run-liveness")
    liveness_run.add_argument("database", type=Path)
    liveness_run.add_argument("--max-candidates", type=int, default=100)
    liveness_run.add_argument("--crawl-priority", type=float, default=0.0)
    liveness_run.add_argument("--live-recheck-hours", type=float, default=168.0)
    crawl_run = commands.add_parser("run-crawl")
    crawl_run.add_argument("database", type=Path)
    crawl_run.add_argument("--max-jobs", type=int, default=10)
    crawl_run.add_argument("--owner", default="local-cli")
    crawl_run.add_argument("--user-agent", default="OpenSearchLocalBot")
    serve = commands.add_parser("serve")
    serve.add_argument("root", type=Path, help="published generation root")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--pinned-key",
        action="append",
        default=[],
        metavar="KEY_ID=BASE64_PUBLIC_KEY",
        help="pinned Ed25519 release key; repeat for key rotation",
    )
    serve.add_argument(
        "--allow-unsigned-local",
        action="store_true",
        help="allow a developer-built unsigned generation; never use for downloaded artifacts",
    )
    serve.add_argument(
        "--enable-local-feedback",
        action="store_true",
        help="enable process-local opt-in feedback; requires --feedback-salt-hex",
    )
    serve.add_argument(
        "--feedback-salt-hex",
        help="stable secret hex salt for local feedback query classes",
    )
    serve.add_argument(
        "--feedback-db",
        type=Path,
        help="optional separate SQLite database for persistent local feedback aggregates",
    )
    args = parser.parse_args()
    if args.command == "canonicalize":
        url = canonicalize_url(args.url)
        print(f"canonical_url={url.value}")
        print(f"url_id={UrlId.from_canonical_url(url).hex}")
    elif args.command == "init-state":
        CrawlStateDatabase(args.database).initialize()
        print(f"initialized={args.database}")
    elif args.command == "inspect-state":
        inspection = CrawlStateDatabase(args.database).inspect()
        print(f"schema_version={inspection.schema_version}")
        print(f"frontier_depth={inspection.frontier_depth}")
        for state, count in sorted(inspection.candidates_by_state.items()):
            print(f"candidates_{state}={count}")
    elif args.command == "backup-state":
        destination = CrawlStateDatabase(args.database).backup_to(args.destination)
        print(f"backup={destination}")
    elif args.command == "seed":
        result = _seed_urls(args.database, tuple(args.urls), args.source)
        print(f"submitted={result.submitted}")
        print(f"created={result.created}")
        print(f"existing={result.existing}")
    elif args.command == "ingest-common-crawl":
        stats = _ingest_common_crawl(
            args.database, args.crawl_id, args.url_pattern, args.partition, args.match_type
        )
        print(f"lines_seen={stats.lines_seen}")
        print(f"candidates_written={stats.candidates_written}")
        print(f"duplicate_or_existing={stats.duplicate_or_existing}")
        print(f"rejected={stats.rejected}")
        print(f"duration_seconds={stats.duration_seconds:.6f}")
        print(f"lines_per_second={stats.lines_per_second:.6f}")
    elif args.command == "list-common-crawl":
        collections = _list_common_crawl(args.limit)
        for collection in collections:
            print(f"crawl_id={collection.crawl_id}\tname={collection.name}\tindex_url={collection.index_url}")
    elif args.command == "build-index":
        result = _build_index(
            args.database,
            args.root,
            args.generation,
            args.max_documents,
            args.shard_count,
            args.target_shard_bytes,
        )
        print(f"generation={result.manifest.generation}")
        print(f"documents={result.document_count}")
        print(f"shards={result.shard_count}")
    elif args.command == "run-liveness":
        stats = _run_liveness(args.database, args.max_candidates, args.crawl_priority, args.live_recheck_hours)
        print(f"checked={stats.checked}")
        print(f"promoted_to_frontier={stats.promoted_to_frontier}")
        print(f"unavailable={stats.unavailable}")
    elif args.command == "run-crawl":
        stats = _run_crawl(args.database, args.owner, args.max_jobs, args.user_agent)
        print(f"attempted={stats.attempted}")
        print(f"completed={stats.completed}")
        print(f"requeued={stats.requeued}")
        print(f"idle={stats.idle}")
    else:
        _serve(
            args.root,
            args.host,
            args.port,
            args.allow_unsigned_local,
            args.pinned_key,
            args.enable_local_feedback,
            args.feedback_salt_hex,
            args.feedback_db,
        )


def _serve(
    root: Path,
    host: str,
    port: int,
    allow_unsigned_local: bool,
    pinned_keys: list[str] | None = None,
    enable_local_feedback: bool = False,
    feedback_salt_hex: str | None = None,
    feedback_database: Path | None = None,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    roots = _parse_pinned_keys(pinned_keys or [])
    if not allow_unsigned_local and not roots:
        raise ValueError("at least one --pinned-key is required unless --allow-unsigned-local is set")
    verifier = None if allow_unsigned_local else PinnedKeyVerifier(roots)
    service = load_active_search_service(root, require_signatures=not allow_unsigned_local, verifier=verifier)
    if enable_local_feedback:
        if feedback_salt_hex is None:
            raise ValueError("--feedback-salt-hex is required with --enable-local-feedback")
        try:
            feedback_salt = bytes.fromhex(feedback_salt_hex)
        except ValueError as exc:
            raise ValueError("feedback salt must be hexadecimal") from exc
        if len(feedback_salt) < 16:
            raise ValueError("feedback salt must contain at least 16 bytes")
        service.feedback = SqliteFeedbackAggregator(feedback_database or root / "feedback.sqlite3")
        service.feedback_salt = feedback_salt
    elif feedback_salt_hex is not None or feedback_database is not None:
        raise ValueError("feedback options require --enable-local-feedback")
    app = create_app(service, generation=service.planner.metadata.generation)
    import uvicorn

    uvicorn.run(app, host=host, port=port)


def _build_index(
    database_path: Path,
    root: Path,
    generation: str,
    max_documents: int,
    shard_count: int,
    target_shard_payload_bytes: int | None = None,
) -> IndexBuildResult:
    database = CrawlStateDatabase(database_path)
    database.initialize()
    return LocalIndexBuilder(CrawledDocumentRepository(database), CandidateRepository(database)).build(
        root,
        generation,
        max_documents=max_documents,
        shard_count=shard_count,
        target_shard_payload_bytes=target_shard_payload_bytes,
    )


def _seed_urls(database_path: Path, urls: tuple[str, ...], source: str) -> SeedResult:
    if not urls:
        raise ValueError("at least one URL is required")
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    created = 0
    for url in urls:
        _url_id, was_created = candidates.upsert_with_result(canonicalize_url(url), source)
        created += int(was_created)
    return SeedResult(len(urls), created, len(urls) - created)


def _ingest_common_crawl(
    database_path: Path,
    crawl_id: str,
    url_pattern: str,
    partition: str,
    match_type: str,
    *,
    client: CommonCrawlIndexClient | None = None,
) -> IngestionStats:
    database = CrawlStateDatabase(database_path)
    database.initialize()
    CommonCrawlPlanRepository(database).record(crawl_id, partition, url_pattern, match_type)
    source = client or CommonCrawlIndexClient()
    lines = source.stream_index(crawl_id, url_pattern, match_type=match_type)
    return CommonCrawlIngestor(CandidateRepository(database), IngestionProgressRepository(database)).ingest_partition(
        lines, crawl_id=crawl_id, partition=partition
    )


def _list_common_crawl(limit: int, *, client: CommonCrawlIndexClient | None = None):
    if limit <= 0:
        raise ValueError("limit must be positive")
    return (client or CommonCrawlIndexClient()).list_collections()[:limit]


def _run_liveness(
    database_path: Path,
    max_candidates: int,
    crawl_priority: float,
    live_recheck_hours: float = 168.0,
    *,
    probe: SafeHttpProbe | None = None,
) -> LivenessRunStats:
    if live_recheck_hours <= 0:
        raise ValueError("live_recheck_hours must be positive")
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    worker = LivenessWorker(candidates, LivenessRepository(database), probe=probe)
    return LivenessRunner(
        candidates,
        worker,
        FrontierRepository(database),
        crawl_priority=crawl_priority,
        live_recheck_interval=timedelta(hours=live_recheck_hours),
    ).run(max_candidates=max_candidates)


def _run_crawl(
    database_path: Path,
    owner: str,
    max_jobs: int,
    user_agent: str,
    *,
    probe: SafeHttpProbe | None = None,
) -> CrawlRunStats:
    database = CrawlStateDatabase(database_path)
    database.initialize()
    candidates = CandidateRepository(database)
    frontier = FrontierRepository(database)
    crawler_probe = probe or SafeHttpProbe()
    robots = RobotsManager(HostRepository(database), crawler_probe)
    worker = CrawlerWorker(
        frontier,
        candidates,
        crawler_probe,
        CrawlResultProcessor(candidates),
        document_store=CrawledDocumentRepository(database),
        robots_check=lambda url: robots.check(url, user_agent),
        robots_delay=lambda url: robots.crawl_delay(url, user_agent),
    )
    return CrawlerRunner(worker).run(owner, max_jobs=max_jobs)


def _parse_pinned_keys(values: list[str]) -> dict[str, str]:
    roots: dict[str, str] = {}
    for value in values:
        key_id, separator, encoded = value.partition("=")
        if not separator or not key_id or not encoded or key_id in roots:
            raise ValueError("--pinned-key must be unique KEY_ID=BASE64_PUBLIC_KEY")
        roots[key_id] = encoded
    return roots
