"""Disk-backed PageRank for rebuilds that must not retain a corpus graph in RAM."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from time import perf_counter

from .identity import UrlId
from .observability import log_event


@dataclass(frozen=True, slots=True)
class DiskPageRankResult:
    nodes: int
    iterations: int
    converged: bool
    delta: float


class DiskPageRank:
    """All nodes, unique edges, degrees, and iteration vectors live in SQLite.

    The caller owns the temporary database and bounds its page cache. SQLite
    temporary storage must be FILE, so aggregation sorts spill to disk.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        connection.execute("CREATE TABLE rank_nodes(id BLOB PRIMARY KEY, degree INTEGER DEFAULT 0, score REAL, next_score REAL)")
        connection.execute("CREATE TABLE rank_edges(source BLOB, target BLOB, PRIMARY KEY(source, target)) WITHOUT ROWID")
        connection.execute("CREATE INDEX rank_targets ON rank_edges(target, source)")

    def add_nodes(self, nodes: Iterable[UrlId]) -> None:
        self.connection.executemany("INSERT OR IGNORE INTO rank_nodes(id) VALUES (?)", ((node.value,) for node in nodes))

    def add_edges(self, edges: Iterable[tuple[UrlId, UrlId]]) -> None:
        self.connection.executemany(
            """INSERT OR IGNORE INTO rank_edges SELECT ?, ?
               WHERE EXISTS(SELECT 1 FROM rank_nodes WHERE id=?)
                 AND EXISTS(SELECT 1 FROM rank_nodes WHERE id=?)""",
            ((source.value, target.value, source.value, target.value) for source, target in edges),
        )

    def compute(
        self, *, damping: float = 0.85, tolerance: float = 1e-10, max_iterations: int = 100,
    ) -> DiskPageRankResult:
        if not 0 < damping < 1 or tolerance <= 0 or max_iterations <= 0:
            raise ValueError("invalid PageRank parameters")
        connection = self.connection
        count = connection.execute("SELECT COUNT(*) FROM rank_nodes").fetchone()[0]
        if not count:
            return DiskPageRankResult(0, 0, True, 0.0)
        started = perf_counter()
        connection.execute("UPDATE rank_nodes SET score=?, degree=(SELECT COUNT(*) FROM rank_edges WHERE source=rank_nodes.id)", (1.0 / count,))
        connection.execute("CREATE TABLE rank_incoming(id BLOB PRIMARY KEY, amount REAL)")
        delta = 0.0
        for iteration in range(1, max_iterations + 1):
            dangling = connection.execute("SELECT COALESCE(SUM(score), 0) FROM rank_nodes WHERE degree=0").fetchone()[0]
            base = (1.0 - damping + damping * dangling) / count
            connection.execute("DELETE FROM rank_incoming")
            connection.execute("""INSERT INTO rank_incoming
                SELECT edges.target, SUM(nodes.score / nodes.degree)
                FROM rank_edges AS edges JOIN rank_nodes AS nodes ON nodes.id=edges.source
                GROUP BY edges.target""")
            connection.execute("""UPDATE rank_nodes SET next_score=? + ? * COALESCE(
                (SELECT amount FROM rank_incoming WHERE id=rank_nodes.id), 0)""", (base, damping))
            delta = connection.execute("SELECT SUM(ABS(next_score-score)) FROM rank_nodes").fetchone()[0]
            connection.execute("UPDATE rank_nodes SET score=next_score")
            # Bound the rollback journal, release transaction pages each iteration.
            connection.commit()
            log_event(logging.getLogger(__name__), "rebuild_pagerank_iteration", nodes=count, iteration=iteration, delta=delta)
            if delta <= tolerance:
                break
        result = DiskPageRankResult(count, iteration, delta <= tolerance, delta)
        log_event(logging.getLogger(__name__), "rebuild_pagerank_completed", nodes=count,
                  iterations=iteration, converged=result.converged, duration_ms=(perf_counter() - started) * 1000)
        return result

    def score_batches(self, *, batch_size: int = 1_000) -> Iterator[dict[str, float]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        cursor = self.connection.execute("SELECT id, score FROM rank_nodes ORDER BY id")
        while rows := cursor.fetchmany(batch_size):
            yield {bytes(identifier).hex(): float(score) for identifier, score in rows}
