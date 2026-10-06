"""Typed JSON payloads for immutable URL, document, graph, ranking, and metadata shards."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .graph import HyperlinkGraph
from .identity import UrlId
from .storage import (
    ArtifactValidationError,
    ShardHeader,
    read_shard,
    write_shard,
    write_shard_stream,
)

PAYLOAD_SCHEMA_VERSION = 1
_REFERENCED_VERSIONS = {"canonicalization": 1, "url_id": 1}


@dataclass(frozen=True, slots=True)
class UrlRecord:
    url_id: UrlId
    canonical_url: str


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    url_id: UrlId
    canonical_url: str
    title: str
    body: str
    content_hash: str
    language: str


@dataclass(frozen=True, slots=True)
class RankingRecord:
    url_id: UrlId
    score: float


def write_url_shard(path: Path, records: Sequence[UrlRecord], *, shard_id: str, generation: str) -> ShardHeader:
    return _write_records(path, "url", shard_id, generation, records, _url_record_to_json)


def read_url_shard(path: Path) -> tuple[UrlRecord, ...]:
    return _read_records(path, "url", _url_record_from_json)


def write_document_shard(
    path: Path, records: Sequence[DocumentRecord], *, shard_id: str, generation: str
) -> ShardHeader:
    return _write_records(path, "document", shard_id, generation, records, _document_record_to_json)


def read_document_shard(path: Path) -> tuple[DocumentRecord, ...]:
    return _read_records(path, "document", _document_record_from_json)


def write_graph_shard(path: Path, graph: HyperlinkGraph, *, shard_id: str, generation: str) -> ShardHeader:
    return write_shard_stream(
        path,
        _graph_payload_chunks(graph),
        shard_type="graph",
        shard_id=shard_id,
        generation=generation,
        referenced_versions=_REFERENCED_VERSIONS,
    )


def _graph_payload_chunks(graph: HyperlinkGraph):
    yield b'{"body":'
    yield from graph.iter_payload_chunks()
    yield b',"schema_version":1,"type":"graph"}'


def read_graph_shard(path: Path) -> HyperlinkGraph:
    header, body = _read_typed_payload(path, "graph")
    del header
    try:
        return HyperlinkGraph.from_payload(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("invalid graph payload") from exc


def write_ranking_shard(
    path: Path, records: Sequence[RankingRecord], *, shard_id: str, generation: str
) -> ShardHeader:
    return _write_records(path, "ranking", shard_id, generation, records, _ranking_record_to_json)


def read_ranking_shard(path: Path) -> tuple[RankingRecord, ...]:
    return _read_records(path, "ranking", _ranking_record_from_json)


def write_metadata_shard(
    path: Path, metadata: Mapping[str, str | int | float | bool], *, shard_id: str, generation: str
) -> ShardHeader:
    if any(not isinstance(key, str) or not key for key in metadata):
        raise ValueError("metadata keys must be non-empty strings")
    if any(not isinstance(value, (str, int, float, bool)) for value in metadata.values()):
        raise ValueError("metadata values must be scalar JSON values")
    return write_shard(
        path,
        _encode("metadata", dict(sorted(metadata.items()))),
        shard_type="metadata",
        shard_id=shard_id,
        generation=generation,
        referenced_versions=_REFERENCED_VERSIONS,
    )


def read_metadata_shard(path: Path) -> dict[str, str | int | float | bool]:
    _header, body = _read_typed_payload(path, "metadata")
    if not isinstance(body, dict) or any(
        not isinstance(key, str) or not isinstance(value, (str, int, float, bool))
        for key, value in body.items()
    ):
        raise ArtifactValidationError("invalid metadata payload")
    return body


def _write_records(path: Path, payload_type: str, shard_id: str, generation: str, records: Sequence[object], encoder) -> ShardHeader:
    return write_shard(
        path,
        _encode(payload_type, [encoder(record) for record in records]),
        shard_type=payload_type,
        shard_id=shard_id,
        generation=generation,
        referenced_versions=_REFERENCED_VERSIONS,
    )


def _read_records(path: Path, payload_type: str, decoder):
    _header, body = _read_typed_payload(path, payload_type)
    if not isinstance(body, list):
        raise ArtifactValidationError(f"invalid {payload_type} payload")
    try:
        records = tuple(decoder(record) for record in body)
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"invalid {payload_type} payload") from exc
    if len({record.url_id for record in records}) != len(records):
        raise ArtifactValidationError(f"duplicate UrlId in {payload_type} payload")
    return records


def _read_typed_payload(path: Path, expected_type: str) -> tuple[ShardHeader, object]:
    header, payload = read_shard(path)
    if header.shard_type != expected_type:
        raise ArtifactValidationError(f"expected {expected_type} shard")
    try:
        envelope = json.loads(payload)
        if envelope["schema_version"] != PAYLOAD_SCHEMA_VERSION or envelope["type"] != expected_type:
            raise ValueError("payload type/version mismatch")
        return header, envelope["body"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"invalid {expected_type} payload") from exc


def _encode(payload_type: str, body: object) -> bytes:
    return json.dumps(
        {"schema_version": PAYLOAD_SCHEMA_VERSION, "type": payload_type, "body": body},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _url_record_to_json(record: UrlRecord) -> dict[str, str]:
    if not record.canonical_url:
        raise ValueError("canonical URL must not be empty")
    return {"url_id": record.url_id.hex, "canonical_url": record.canonical_url}


def _url_record_from_json(value: object) -> UrlRecord:
    if not isinstance(value, dict):
        raise TypeError("URL record must be an object")
    return UrlRecord(UrlId(bytes.fromhex(value["url_id"])), str(value["canonical_url"]))


def _document_record_to_json(record: DocumentRecord) -> dict[str, object]:
    if not record.canonical_url or not record.content_hash or not record.language:
        raise ValueError("document identity fields must not be empty")
    return {"url_id": record.url_id.hex, **asdict(record) | {"url_id": record.url_id.hex}}


def _document_record_from_json(value: object) -> DocumentRecord:
    if not isinstance(value, dict):
        raise TypeError("document record must be an object")
    return DocumentRecord(
        UrlId(bytes.fromhex(value["url_id"])),
        str(value["canonical_url"]),
        str(value["title"]),
        str(value["body"]),
        str(value["content_hash"]),
        str(value["language"]),
    )


def _ranking_record_to_json(record: RankingRecord) -> dict[str, object]:
    if not math.isfinite(record.score) or record.score < 0:
        raise ValueError("ranking score must be finite and non-negative")
    return {"url_id": record.url_id.hex, "score": record.score}


def _ranking_record_from_json(value: object) -> RankingRecord:
    if not isinstance(value, dict):
        raise TypeError("ranking record must be an object")
    score = float(value["score"])
    if not math.isfinite(score) or score < 0:
        raise ValueError("ranking score must be finite and non-negative")
    return RankingRecord(UrlId(bytes.fromhex(value["url_id"])), score)
