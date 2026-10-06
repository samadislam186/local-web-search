"""SQLite/WAL mutable crawl state behind narrow repository interfaces."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .canonicalization import CanonicalUrl
from .errors import DatabaseSchemaError, InvalidLifecycleTransition, UrlCollisionError
from .identity import UrlId


class UrlState(StrEnum):
    DISCOVERED = "discovered"
    CANONICALIZED = "canonicalized"
    LIVENESS_PENDING = "liveness_pending"
    LIVE = "live"
    LIVE_BUT_BLOCKED = "live_but_blocked"
    LIVE_BUT_UNUSABLE = "live_but_unusable"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    DEAD = "dead"
    UNKNOWN = "unknown"
    CRAWLABLE = "crawlable"
    CRAWLED = "crawled"
    INDEXED = "indexed"


@dataclass(frozen=True, slots=True)
class FrontierLease:
    url_id: UrlId
    attempt_count: int


@dataclass(frozen=True, slots=True)
class UrlCollisionRecord:
    url_id: UrlId
    stored_canonical_url: str
    incoming_canonical_url: str
    source: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class CandidateStateTransition:
    url_id: UrlId
    from_state: UrlState | None
    to_state: UrlState
    reason: str | None
    transitioned_at: str


@dataclass(frozen=True, slots=True)
class CrawlStateInspection:
    schema_version: int
    candidates_by_state: dict[str, int]
    frontier_depth: int


_TRANSITIONS: dict[UrlState, frozenset[UrlState]] = {
    UrlState.DISCOVERED: frozenset({UrlState.CANONICALIZED}),
    UrlState.CANONICALIZED: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.LIVENESS_PENDING: frozenset({UrlState.LIVE, UrlState.LIVE_BUT_BLOCKED, UrlState.LIVE_BUT_UNUSABLE, UrlState.TEMPORARILY_UNAVAILABLE, UrlState.DEAD, UrlState.UNKNOWN}),
    UrlState.LIVE: frozenset(
        {
            UrlState.LIVENESS_PENDING,
            UrlState.CRAWLABLE,
            UrlState.LIVE_BUT_BLOCKED,
            UrlState.LIVE_BUT_UNUSABLE,
            UrlState.TEMPORARILY_UNAVAILABLE,
            UrlState.DEAD,
        }
    ),
    UrlState.LIVE_BUT_BLOCKED: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.LIVE_BUT_UNUSABLE: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.TEMPORARILY_UNAVAILABLE: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.DEAD: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.UNKNOWN: frozenset({UrlState.LIVENESS_PENDING}),
    UrlState.CRAWLABLE: frozenset(
        {UrlState.CRAWLED, UrlState.LIVE_BUT_BLOCKED, UrlState.LIVE_BUT_UNUSABLE, UrlState.TEMPORARILY_UNAVAILABLE}
    ),
    UrlState.CRAWLED: frozenset({UrlState.INDEXED, UrlState.CRAWLABLE}),
    UrlState.INDEXED: frozenset({UrlState.CRAWLABLE}),
}


def can_transition(current: UrlState, target: UrlState) -> bool:
    """Return whether the versioned URL lifecycle permits this state change."""
    return target in _TRANSITIONS[current]


class CrawlStateDatabase:
    SCHEMA_VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("""CREATE TABLE IF NOT EXISTS candidates (
                url_id BLOB PRIMARY KEY CHECK(length(url_id) = 16),
                canonical_url TEXT NOT NULL UNIQUE,
                canonicalization_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state_reason TEXT
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS candidate_provenance (
                id INTEGER PRIMARY KEY,
                url_id BLOB NOT NULL REFERENCES candidates(url_id),
                source TEXT NOT NULL,
                source_record_id TEXT,
                observed_at TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(url_id, source, source_record_id)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS ingestion_progress (
                source TEXT NOT NULL,
                crawl_id TEXT NOT NULL,
                partition TEXT NOT NULL,
                line_number INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source, crawl_id, partition)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS liveness (
                url_id BLOB PRIMARY KEY REFERENCES candidates(url_id),
                dns_last_checked TEXT,
                dns_status TEXT,
                http_last_checked TEXT,
                http_status INTEGER,
                tls_status TEXT,
                redirect_target TEXT,
                failure_reason TEXT,
                retry_after TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS hosts (
                host TEXT PRIMARY KEY,
                next_allowed_at TEXT,
                active_leases INTEGER NOT NULL DEFAULT 0,
                robots_body TEXT,
                robots_fetched_at TEXT,
                robots_expires_at TEXT,
                updated_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS frontier (
                url_id BLOB PRIMARY KEY REFERENCES candidates(url_id),
                host TEXT NOT NULL,
                priority REAL NOT NULL,
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS crawl_attempts (
                id INTEGER PRIMARY KEY,
                url_id BLOB NOT NULL REFERENCES candidates(url_id),
                host TEXT NOT NULL,
                owner TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                error TEXT
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS crawled_documents (
                url_id BLOB PRIMARY KEY REFERENCES candidates(url_id),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                headings_json TEXT NOT NULL,
                anchor_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                simhash_hex TEXT NOT NULL,
                language TEXT NOT NULL,
                crawled_at TEXT NOT NULL,
                indexed_generation TEXT
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS url_collisions (
                id INTEGER PRIMARY KEY,
                url_id BLOB NOT NULL CHECK(length(url_id) = 16),
                stored_canonical_url TEXT NOT NULL,
                incoming_canonical_url TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS candidate_state_transitions (
                id INTEGER PRIMARY KEY,
                url_id BLOB NOT NULL REFERENCES candidates(url_id),
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT,
                transitioned_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS common_crawl_plans (
                crawl_id TEXT NOT NULL,
                partition TEXT NOT NULL,
                url_pattern TEXT NOT NULL,
                match_type TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                PRIMARY KEY(crawl_id, partition)
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS frontier_ready ON frontier(available_at, priority DESC)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS crawled_documents_recent ON crawled_documents(crawled_at DESC, url_id)"
            )
            liveness_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(liveness)")}
            if "retry_count" not in liveness_columns:
                connection.execute("ALTER TABLE liveness ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            # SQLite UNIQUE treats NULLs as distinct. Normalize legacy rows so
            # source records without an upstream ID remain idempotent.
            connection.execute("UPDATE candidate_provenance SET source_record_id='' WHERE source_record_id IS NULL")
            connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    def inspect(self) -> CrawlStateInspection:
        with self.connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            states = {
                str(row["state"]): int(row["count"])
                for row in connection.execute("SELECT state, COUNT(*) AS count FROM candidates GROUP BY state")
            }
            frontier_depth = int(connection.execute("SELECT COUNT(*) FROM frontier").fetchone()[0])
        return CrawlStateInspection(version, states, frontier_depth)

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent SQLite backup and atomically replace the destination."""
        if not self.path.is_file():
            raise FileNotFoundError(f"crawl state does not exist: {self.path}")
        if self.path.resolve() == destination.resolve():
            raise ValueError("backup destination must differ from source database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self.connection() as source:
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                finally:
                    target.close()
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            connection.close()
            raise DatabaseSchemaError(
                f"crawl-state schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
            )
        try:
            yield connection
        finally:
            connection.close()


class CandidateRepository:
    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def upsert(
        self,
        canonical_url: CanonicalUrl,
        source: str,
        *,
        source_record_id: str | None = None,
        observed_at: str | None = None,
        metadata_json: str = "{}",
    ) -> UrlId:
        return self.upsert_with_result(
            canonical_url,
            source,
            source_record_id=source_record_id,
            observed_at=observed_at,
            metadata_json=metadata_json,
        )[0]

    def upsert_with_result(
        self,
        canonical_url: CanonicalUrl,
        source: str,
        *,
        source_record_id: str | None = None,
        observed_at: str | None = None,
        metadata_json: str = "{}",
    ) -> tuple[UrlId, bool]:
        if not source.strip():
            raise ValueError("candidate source must not be empty")
        url_id = UrlId.from_canonical_url(canonical_url)
        now = _now()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT canonical_url FROM candidates WHERE url_id = ?", (url_id.value,)).fetchone()
            if existing is not None and existing["canonical_url"] != canonical_url.value:
                connection.execute(
                    """INSERT INTO url_collisions(
                        url_id, stored_canonical_url, incoming_canonical_url, source, observed_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (url_id.value, existing["canonical_url"], canonical_url.value, source, now),
                )
                connection.execute("COMMIT")
                raise UrlCollisionError(f"UrlId collision for {url_id.hex}")
            candidate_exists = connection.execute(
                "SELECT 1 FROM candidates WHERE canonical_url = ?", (canonical_url.value,)
            ).fetchone() is not None
            connection.execute(
                """INSERT INTO candidates(url_id, canonical_url, canonicalization_version, state, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(canonical_url) DO UPDATE SET updated_at = excluded.updated_at""",
                (url_id.value, canonical_url.value, canonical_url.version, UrlState.CANONICALIZED, source, now, now),
            )
            if not candidate_exists:
                connection.execute(
                    """INSERT INTO candidate_state_transitions(
                        url_id, from_state, to_state, reason, transitioned_at
                    ) VALUES (?, NULL, ?, ?, ?)""",
                    (url_id.value, UrlState.CANONICALIZED, "candidate_created", now),
                )
            connection.execute(
                """INSERT INTO candidate_provenance(url_id, source, source_record_id, observed_at, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url_id, source, source_record_id) DO NOTHING""",
                (url_id.value, source, source_record_id or "", observed_at, metadata_json, now),
            )
            connection.execute("COMMIT")
        return url_id, not candidate_exists

    def collision_records(self) -> tuple[UrlCollisionRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT url_id, stored_canonical_url, incoming_canonical_url, source, observed_at
                   FROM url_collisions ORDER BY id"""
            ).fetchall()
        return tuple(
            UrlCollisionRecord(
                UrlId(bytes(row["url_id"])),
                str(row["stored_canonical_url"]),
                str(row["incoming_canonical_url"]),
                str(row["source"]),
                str(row["observed_at"]),
            )
            for row in rows
        )

    def transition(self, url_id: UrlId, target: UrlState, reason: str | None = None) -> None:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM candidates WHERE url_id = ?", (url_id.value,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(f"unknown UrlId {url_id.hex}")
            current = UrlState(row["state"])
            if not can_transition(current, target):
                connection.execute("ROLLBACK")
                raise InvalidLifecycleTransition(f"cannot transition {current} to {target}")
            transitioned_at = _now()
            connection.execute(
                "UPDATE candidates SET state=?, state_reason=?, updated_at=? WHERE url_id=?",
                (target, reason, transitioned_at, url_id.value),
            )
            connection.execute(
                """INSERT INTO candidate_state_transitions(
                    url_id, from_state, to_state, reason, transitioned_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (url_id.value, current, target, reason, transitioned_at),
            )
            connection.execute("COMMIT")

    def get_state(self, url_id: UrlId) -> UrlState:
        with self.database.connection() as connection:
            row = connection.execute("SELECT state FROM candidates WHERE url_id = ?", (url_id.value,)).fetchone()
        if row is None:
            raise KeyError(f"unknown UrlId {url_id.hex}")
        return UrlState(row["state"])

    def transition_history(self, url_id: UrlId) -> tuple[CandidateStateTransition, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT from_state, to_state, reason, transitioned_at
                   FROM candidate_state_transitions WHERE url_id=? ORDER BY id""",
                (url_id.value,),
            ).fetchall()
        return tuple(
            CandidateStateTransition(
                url_id,
                None if row["from_state"] is None else UrlState(row["from_state"]),
                UrlState(row["to_state"]),
                row["reason"],
                str(row["transitioned_at"]),
            )
            for row in rows
        )

    def canonical_url(self, url_id: UrlId) -> CanonicalUrl:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT canonical_url, canonicalization_version FROM candidates WHERE url_id = ?", (url_id.value,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown UrlId {url_id.hex}")
        return CanonicalUrl(str(row["canonical_url"]), int(row["canonicalization_version"]))

    def ids_in_state(self, state: UrlState, *, limit: int) -> tuple[UrlId, ...]:
        """Return a bounded, stable batch for a lifecycle worker."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT url_id FROM candidates WHERE state=? ORDER BY created_at, url_id LIMIT ?",
                (state, limit),
            ).fetchall()
        return tuple(UrlId(bytes(row["url_id"])) for row in rows)

    def ids_due_for_liveness(
        self, now: str, *, limit: int, live_recheck_before: str | None = None
    ) -> tuple[UrlId, ...]:
        """Return initial, retryable, and periodically recheckable live candidates."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT candidates.url_id FROM candidates
                   LEFT JOIN liveness ON liveness.url_id=candidates.url_id
                   WHERE candidates.state=?
                      OR (candidates.state=? AND (liveness.retry_after IS NULL OR liveness.retry_after <= ?))
                      OR (candidates.state=? AND (liveness.retry_after IS NULL OR liveness.retry_after <= ?))
                      OR (? IS NOT NULL AND candidates.state=? AND (
                        liveness.http_last_checked IS NULL OR liveness.http_last_checked <= ?
                      ))
                   ORDER BY CASE candidates.state WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,
                     COALESCE(liveness.http_last_checked, candidates.created_at), candidates.url_id LIMIT ?""",
                (
                    UrlState.CANONICALIZED,
                    UrlState.TEMPORARILY_UNAVAILABLE,
                    now,
                    UrlState.UNKNOWN,
                    now,
                    live_recheck_before,
                    UrlState.LIVE,
                    live_recheck_before,
                    UrlState.CANONICALIZED,
                    UrlState.TEMPORARILY_UNAVAILABLE,
                    UrlState.UNKNOWN,
                    limit,
                ),
            ).fetchall()
        return tuple(UrlId(bytes(row["url_id"])) for row in rows)


class IngestionProgressRepository:
    """Resumable source/partition checkpoints committed with each processed record."""

    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def last_committed_line(self, source: str, crawl_id: str, partition: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT line_number FROM ingestion_progress WHERE source=? AND crawl_id=? AND partition=?",
                (source, crawl_id, partition),
            ).fetchone()
        return 0 if row is None else int(row["line_number"])

    def commit(self, source: str, crawl_id: str, partition: str, line_number: int, status: str = "running") -> None:
        if line_number < 0:
            raise ValueError("line_number must not be negative")
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO ingestion_progress(source, crawl_id, partition, line_number, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, crawl_id, partition) DO UPDATE SET
                     line_number=excluded.line_number, status=excluded.status, updated_at=excluded.updated_at
                   WHERE excluded.line_number >= ingestion_progress.line_number""",
                (source, crawl_id, partition, line_number, status, _now()),
            )


@dataclass(frozen=True, slots=True)
class CommonCrawlPlan:
    crawl_id: str
    partition: str
    url_pattern: str
    match_type: str
    selected_at: str


class CommonCrawlPlanRepository:
    """Records reproducible Common Crawl selection inputs before ingestion."""

    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def record(self, crawl_id: str, partition: str, url_pattern: str, match_type: str) -> None:
        if not crawl_id or not partition or not url_pattern or match_type not in {"domain", "prefix", "host", "exact"}:
            raise ValueError("invalid Common Crawl selection plan")
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO common_crawl_plans(crawl_id, partition, url_pattern, match_type, selected_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(crawl_id, partition) DO UPDATE SET
                     url_pattern=excluded.url_pattern, match_type=excluded.match_type, selected_at=excluded.selected_at""",
                (crawl_id, partition, url_pattern, match_type, _now()),
            )

    def get(self, crawl_id: str, partition: str) -> CommonCrawlPlan | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT crawl_id, partition, url_pattern, match_type, selected_at
                   FROM common_crawl_plans WHERE crawl_id=? AND partition=?""",
                (crawl_id, partition),
            ).fetchone()
        if row is None:
            return None
        return CommonCrawlPlan(
            str(row["crawl_id"]),
            str(row["partition"]),
            str(row["url_pattern"]),
            str(row["match_type"]),
            str(row["selected_at"]),
        )


class LivenessRepository:
    """Stores independently observable DNS, TLS, and HTTP liveness outcomes."""

    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def record_dns(
        self, url_id: UrlId, status: str, failure_reason: str | None = None, *, recorded_at: str | None = None
    ) -> None:
        self._upsert(url_id, dns_last_checked=recorded_at or _now(), dns_status=status, failure_reason=failure_reason)

    def record_http(
        self,
        url_id: UrlId,
        status: int | None,
        *,
        tls_status: str | None = None,
        redirect_target: str | None = None,
        failure_reason: str | None = None,
        retry_after: str | None = None,
        retry_count: int | None = None,
        recorded_at: str | None = None,
    ) -> None:
        if retry_count is not None and retry_count < 0:
            raise ValueError("retry_count must not be negative")
        self._upsert(
            url_id,
            http_last_checked=recorded_at or _now(),
            http_status=status,
            tls_status=tls_status,
            redirect_target=redirect_target,
            failure_reason=failure_reason,
            retry_after=retry_after,
            **({"retry_count": retry_count} if retry_count is not None else {}),
        )

    def retry_count(self, url_id: UrlId) -> int:
        with self.database.connection() as connection:
            row = connection.execute("SELECT retry_count FROM liveness WHERE url_id=?", (url_id.value,)).fetchone()
        return int(row["retry_count"]) if row is not None else 0

    def _upsert(self, url_id: UrlId, **fields: object) -> None:
        columns = ["url_id", *fields]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{column}=excluded.{column}" for column in fields)
        values = [url_id.value, *fields.values()]
        with self.database.connection() as connection:
            connection.execute(
                f"INSERT INTO liveness({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(url_id) DO UPDATE SET {assignments}",
                values,
            )


class HostRepository:
    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def save_robots(self, host: str, body: str, expires_at: str) -> None:
        now = _now()
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO hosts(host, robots_body, robots_fetched_at, robots_expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(host) DO UPDATE SET robots_body=excluded.robots_body,
                     robots_fetched_at=excluded.robots_fetched_at, robots_expires_at=excluded.robots_expires_at,
                     updated_at=excluded.updated_at""",
                (host, body, now, expires_at, now),
            )

    def robots(self, host: str) -> tuple[str, str] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT robots_body, robots_expires_at FROM hosts WHERE host=?", (host,)
            ).fetchone()
        if row is None or row["robots_body"] is None:
            return None
        return str(row["robots_body"]), str(row["robots_expires_at"])


class FrontierRepository:
    """Leased priority work queue; scheduling always checks host eligibility."""

    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def enqueue(self, url_id: UrlId, host: str, priority: float, available_at: str | None = None) -> None:
        now = _now()
        with self.database.connection() as connection:
            connection.execute(
                "INSERT INTO hosts(host, updated_at) VALUES (?, ?) ON CONFLICT(host) DO NOTHING",
                (host, now),
            )
            connection.execute(
                """INSERT INTO frontier(url_id, host, priority, available_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url_id) DO UPDATE SET priority=MAX(frontier.priority, excluded.priority),
                     available_at=MIN(frontier.available_at, excluded.available_at), updated_at=excluded.updated_at""",
                (url_id.value, host, priority, available_at or now, now, now),
            )

    def lease_next(self, owner: str, now: str, lease_expires_at: str) -> UrlId | None:
        lease = self.lease_next_details(owner, now, lease_expires_at)
        return None if lease is None else lease.url_id

    def lease_next_details(self, owner: str, now: str, lease_expires_at: str) -> FrontierLease | None:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A dead worker never calls complete(). Recalculate the derived
            # host count from unexpired leases before choosing new work.
            connection.execute(
                """UPDATE hosts SET active_leases=(
                    SELECT COUNT(*) FROM frontier
                    WHERE frontier.host=hosts.host AND frontier.lease_expires_at > ?
                ), updated_at=?""",
                (now, now),
            )
            row = connection.execute(
                """SELECT frontier.url_id FROM frontier
                   LEFT JOIN hosts ON hosts.host=frontier.host
                   WHERE frontier.available_at <= ?
                     AND (frontier.lease_expires_at IS NULL OR frontier.lease_expires_at <= ?)
                     AND (hosts.next_allowed_at IS NULL OR hosts.next_allowed_at <= ?)
                     AND COALESCE(hosts.active_leases, 0) = 0
                   ORDER BY frontier.priority DESC, frontier.available_at ASC LIMIT 1""",
                (now, now, now),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            url_id = UrlId(bytes(row["url_id"]))
            connection.execute(
                "UPDATE frontier SET lease_owner=?, lease_expires_at=?, attempt_count=attempt_count+1, updated_at=? WHERE url_id=?",
                (owner, lease_expires_at, now, url_id.value),
            )
            connection.execute(
                "UPDATE hosts SET active_leases=active_leases+1, updated_at=? WHERE host=(SELECT host FROM frontier WHERE url_id=?)",
                (now, url_id.value),
            )
            attempt = connection.execute("SELECT attempt_count FROM frontier WHERE url_id=?", (url_id.value,)).fetchone()
            connection.execute("COMMIT")
        assert attempt is not None
        return FrontierLease(url_id, int(attempt["attempt_count"]))

    def complete(self, url_id: UrlId, owner: str, next_allowed_at: str, error: str | None = None) -> None:
        now = _now()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT host FROM frontier WHERE url_id=? AND lease_owner=?", (url_id.value, owner)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError("lease not owned by caller")
            connection.execute(
                "INSERT INTO crawl_attempts(url_id, host, owner, attempted_at, error) VALUES (?, ?, ?, ?, ?)",
                (url_id.value, row["host"], owner, now, error),
            )
            connection.execute("DELETE FROM frontier WHERE url_id=?", (url_id.value,))
            connection.execute(
                """INSERT INTO hosts(host, next_allowed_at, active_leases, updated_at) VALUES (?, ?, 0, ?)
                   ON CONFLICT(host) DO UPDATE SET next_allowed_at=excluded.next_allowed_at,
                     active_leases=MAX(hosts.active_leases-1, 0), updated_at=excluded.updated_at""",
                (row["host"], next_allowed_at, now),
            )
            connection.execute("COMMIT")

    def release_for_retry(
        self, url_id: UrlId, owner: str, available_at: str, error: str, *, next_allowed_at: str | None = None
    ) -> None:
        """Release a failed lease without dropping the URL from the frontier."""
        now = _now()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT host FROM frontier WHERE url_id=? AND lease_owner=?", (url_id.value, owner)
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError("lease not owned by caller")
            connection.execute(
                "INSERT INTO crawl_attempts(url_id, host, owner, attempted_at, error) VALUES (?, ?, ?, ?, ?)",
                (url_id.value, row["host"], owner, now, error),
            )
            connection.execute(
                """UPDATE frontier SET lease_owner=NULL, lease_expires_at=NULL, available_at=?, last_error=?, updated_at=?
                   WHERE url_id=?""",
                (available_at, error, now, url_id.value),
            )
            connection.execute(
                """UPDATE hosts SET active_leases=MAX(active_leases-1, 0),
                   next_allowed_at=CASE
                     WHEN ? IS NULL THEN next_allowed_at
                     WHEN next_allowed_at IS NULL OR next_allowed_at < ? THEN ?
                     ELSE next_allowed_at
                   END,
                   updated_at=? WHERE host=?""",
                (next_allowed_at, next_allowed_at, next_allowed_at, now, row["host"]),
            )
            connection.execute("COMMIT")


def _now() -> str:
    return datetime.now(UTC).isoformat()
