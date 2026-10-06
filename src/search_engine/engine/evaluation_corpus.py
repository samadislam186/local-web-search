"""Versioned, reproducible inputs for offline routing and retrieval experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .identity import UrlId
from .index import SearchDocument


@dataclass(frozen=True, slots=True)
class RelevanceJudgment:
    query: str
    url_id: str
    grade: int


@dataclass(frozen=True, slots=True)
class RepresentativeCorpus:
    provenance: str
    sampling_rule: str
    documents: tuple[SearchDocument, ...]
    queries: tuple[str, ...]
    judgments: tuple[RelevanceJudgment, ...]
    format_version: int = 1

    def __post_init__(self) -> None:
        if self.format_version != 1 or not self.provenance.strip() or not self.sampling_rule.strip():
            raise ValueError("corpus provenance, sampling rule, and format version are required")
        document_ids = {document.url_id.hex for document in self.documents}
        if len(document_ids) != len(self.documents) or not self.queries or len(set(self.queries)) != len(self.queries):
            raise ValueError("documents must have unique IDs and queries must be unique and non-empty")
        if any(not query.strip() for query in self.queries):
            raise ValueError("queries must not be empty")
        if any(
            judgment.query not in self.queries or judgment.url_id not in document_ids or judgment.grade < 0
            for judgment in self.judgments
        ):
            raise ValueError("judgments must reference corpus queries/documents with non-negative grades")

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "format_version": self.format_version,
                "provenance": self.provenance,
                "sampling_rule": self.sampling_rule,
                "documents": [{**asdict(document), "url_id": document.url_id.hex} for document in self.documents],
                "queries": self.queries,
                "judgments": [asdict(judgment) for judgment in self.judgments],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes) -> RepresentativeCorpus:
        try:
            data = json.loads(payload)
            documents = tuple(
                SearchDocument(url_id=UrlId(bytes.fromhex(item.pop("url_id"))), **item)
                for item in data["documents"]
            )
            return cls(
                provenance=str(data["provenance"]),
                sampling_rule=str(data["sampling_rule"]),
                documents=documents,
                queries=tuple(str(query) for query in data["queries"]),
                judgments=tuple(RelevanceJudgment(**judgment) for judgment in data["judgments"]),
                format_version=int(data["format_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid representative corpus") from exc
