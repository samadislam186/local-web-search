"""Durable normalized crawl documents awaiting immutable index publication."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .canonicalization import canonicalize_url
from .crawl_processing import CrawledDocument
from .errors import UrlCanonicalizationError
from .identity import UrlId
from .index import SearchDocument
from .state import CrawlStateDatabase, UrlState


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    url_id: UrlId
    distance: int


class CrawledDocumentRepository:
    """Stores parsed text only; raw response bodies never enter mutable state."""

    def __init__(self, database: CrawlStateDatabase) -> None:
        self.database = database

    def save(self, document: CrawledDocument, *, lease_owner: str | None = None,
             lease_attempt: int | None = None, transition_state: bool = False) -> None:
        if (lease_owner is None) != (lease_attempt is None):
            raise ValueError("lease owner and attempt must be supplied together")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if lease_owner is not None and connection.execute(
                "SELECT 1 FROM frontier WHERE url_id=? AND lease_owner=? AND attempt_count=?",
                (document.url_id.value, lease_owner, lease_attempt),
            ).fetchone() is None:
                raise KeyError("crawl lease has been superseded")
            connection.execute(
                """INSERT INTO crawled_documents(
                    url_id, title, body, headings_json, anchor_json, content_hash,
                    simhash_hex, language, crawled_at, indexed_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(url_id) DO UPDATE SET
                    title=excluded.title, body=excluded.body, headings_json=excluded.headings_json,
                    anchor_json=excluded.anchor_json, content_hash=excluded.content_hash,
                    simhash_hex=excluded.simhash_hex, language=excluded.language,
                    crawled_at=excluded.crawled_at, indexed_generation=NULL""",
                (
                    document.url_id.value,
                    document.title,
                    document.body,
                    json.dumps(document.headings, separators=(",", ":")),
                    json.dumps(document.anchor_text, separators=(",", ":")),
                    document.content_hash,
                    f"{document.simhash:016x}",
                    document.language.language or "und",
                    _now(),
                ),
            )
            if transition_state:
                row = connection.execute("SELECT state FROM candidates WHERE url_id=?", (document.url_id.value,)).fetchone()
                current = UrlState(row["state"])
                for target in ((UrlState.CRAWLABLE, UrlState.CRAWLED) if current is UrlState.LIVE else
                               (UrlState.CRAWLED,) if current is UrlState.CRAWLABLE else ()):
                    timestamp = _now()
                    connection.execute(
                        "UPDATE candidates SET state=?, state_reason=?, updated_at=? WHERE url_id=?",
                        (target, "document_persisted", timestamp, document.url_id.value),
                    )
                    connection.execute(
                        "INSERT INTO candidate_state_transitions(url_id, from_state, to_state, reason, transitioned_at) VALUES (?, ?, ?, ?, ?)",
                        (document.url_id.value, current, target, "document_persisted", timestamp),
                    )
                    current = target
            connection.commit()

    def pending_for_index(self, *, limit: int) -> tuple[SearchDocument, ...]:
        return tuple(self.iter_pending_for_index(limit=limit))

    def iter_pending_for_index(self, *, limit: int) -> Iterator[SearchDocument]:
        """Read a consistent pending snapshot, one normalized document at a time."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.database.connection() as connection:
            connection.execute("PRAGMA cache_size=-8192")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("BEGIN")
            rows = connection.execute(
                """SELECT documents.url_id, candidates.canonical_url, documents.title, documents.body,
                          documents.headings_json, documents.anchor_json, documents.crawled_at
                   FROM crawled_documents AS documents
                   JOIN candidates ON candidates.url_id=documents.url_id
                   WHERE documents.indexed_generation IS NULL
                   ORDER BY documents.crawled_at, documents.url_id LIMIT ?""",
                (limit,),
            )
            for row in rows:
                yield SearchDocument(
                    UrlId(bytes(row["url_id"])),
                    str(row["canonical_url"]),
                    str(row["title"]),
                    str(row["body"]),
                    " ".join(json.loads(str(row["headings_json"]))),
                    " ".join(text for _url, text in json.loads(str(row["anchor_json"]))),
                    crawled_at=str(row["crawled_at"]),
                )

    def mark_indexed_versions(
        self, versions: Iterable[tuple[UrlId, str]], generation: str, *, transition_candidates: bool = False,
    ) -> tuple[UrlId, ...]:
        """Checkpoint only the exact crawl versions published, not concurrent recrawls.

        Callers supply bounded batches; the return value contains only committed IDs.
        """
        if not generation:
            raise ValueError("generation must not be empty")
        marked = []
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for url_id, crawled_at in versions:
                cursor = connection.execute(
                    """UPDATE crawled_documents SET indexed_generation=?
                       WHERE url_id=? AND crawled_at=? AND indexed_generation IS NULL""",
                    (generation, url_id.value, crawled_at),
                )
                if cursor.rowcount:
                    marked.append(url_id)
                    if transition_candidates:
                        now = _now()
                        reason = f"generation:{generation}"
                        connection.execute(
                            """INSERT INTO candidate_state_transitions(url_id, from_state, to_state, reason, transitioned_at)
                               SELECT url_id, state, ?, ?, ? FROM candidates WHERE url_id=? AND state=?""",
                            (UrlState.INDEXED, reason, now, url_id.value, UrlState.CRAWLED),
                        )
                        connection.execute(
                            "UPDATE candidates SET state=?, state_reason=?, updated_at=? WHERE url_id=? AND state=?",
                            (UrlState.INDEXED, reason, now, url_id.value, UrlState.CRAWLED),
                        )
            connection.commit()
        return tuple(marked)

    def mark_indexed(self, url_ids: Iterable[UrlId], generation: str) -> None:
        identifiers = tuple(url_id.value for url_id in url_ids)
        if not identifiers:
            return
        if not generation:
            raise ValueError("generation must not be empty")
        placeholders = ", ".join("?" for _ in identifiers)
        with self.database.connection() as connection:
            connection.execute(
                f"UPDATE crawled_documents SET indexed_generation=? WHERE url_id IN ({placeholders})",
                (generation, *identifiers),
            )

    def near_duplicates(
        self,
        simhash: int,
        *,
        max_distance: int = 3,
        max_candidates: int = 1_000,
        exclude_url_id: UrlId | None = None,
    ) -> tuple[NearDuplicate, ...]:
        """Return bounded SimHash matches; callers choose any indexing/ranking policy."""
        if not 0 <= simhash < 1 << 64:
            raise ValueError("simhash must be an unsigned 64-bit integer")
        if not 0 <= max_distance <= 64 or max_candidates <= 0:
            raise ValueError("max_distance and max_candidates are out of range")
        query = "SELECT url_id, simhash_hex FROM crawled_documents"
        parameters: list[object] = []
        if exclude_url_id is not None:
            query += " WHERE url_id != ?"
            parameters.append(exclude_url_id.value)
        query += " ORDER BY crawled_at DESC, url_id LIMIT ?"
        parameters.append(max_candidates)
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        matches = (
            NearDuplicate(
                UrlId(bytes(row["url_id"])),
                (simhash ^ int(str(row["simhash_hex"]), 16)).bit_count(),
            )
            for row in rows
        )
        return tuple(sorted((match for match in matches if match.distance <= max_distance), key=lambda match: (match.distance, match.url_id.value)))

    def link_edges(self, source_ids: Iterable[UrlId]) -> tuple[tuple[UrlId, UrlId], ...]:
        return tuple(self.iter_link_edges(source_ids))

    def iter_link_edges(self, source_ids: Iterable[UrlId]) -> Iterator[tuple[UrlId, UrlId]]:
        """Return canonical outbound edges for persisted source documents."""
        identifiers = tuple(url_id.value for url_id in source_ids)
        if not identifiers:
            return
        placeholders = ", ".join("?" for _ in identifiers)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT url_id, anchor_json FROM crawled_documents WHERE url_id IN ({placeholders})", identifiers
            )
            for row in rows:
                source = UrlId(bytes(row["url_id"]))
                for target, _text in json.loads(str(row["anchor_json"])):
                    try:
                        yield source, UrlId.from_canonical_url(canonicalize_url(target))
                    except UrlCanonicalizationError:
                        continue


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
