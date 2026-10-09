"""Optional Tantivy capability boundary for the project-owned index contract."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import tempfile
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from .identity import UrlId
from .index import SearchDocument, SearchHit
from .snippets import make_snippet
from .storage import MAX_SHARD_PAYLOAD_BYTES, ArtifactValidationError


@dataclass(frozen=True, slots=True)
class TantivyCapability:
    available: bool
    version: str | None
    reason: str | None


class TantivyUnavailableError(RuntimeError):
    """Raised when a deployment selects Tantivy without its optional dependency."""


def tantivy_capability() -> TantivyCapability:
    """Report the optional native backend's availability without importing it eagerly."""
    if importlib.util.find_spec("tantivy") is None:
        return TantivyCapability(False, None, "optional dependency 'tantivy' is not installed")
    try:
        installed_version = version("tantivy")
    except PackageNotFoundError:
        installed_version = None
    return TantivyCapability(True, installed_version, None)


def require_tantivy() -> None:
    """Fail closed with an operator-actionable error when Tantivy is selected."""
    capability = tantivy_capability()
    if not capability.available:
        raise TantivyUnavailableError(capability.reason or "Tantivy is unavailable")


class TantivyIndex:
    """Persistent Tantivy implementation of the project-owned local index contract."""

    _TEXT_FIELDS = ("title", "body", "headings", "url", "anchor", "host", "domain")

    def __init__(self, index: object) -> None:
        self._index = index
        self._temporary = None

    @classmethod
    def build_payload(cls, documents: tuple[SearchDocument, ...], *, temporary_root: Path | None = None) -> bytes:
        """Package native static index files inside the checksumed shard envelope."""
        from .index import Bm25fIndex

        with tempfile.TemporaryDirectory(prefix=".native-build-" if temporary_root else "search-tantivy-build-",
                                         dir=temporary_root) as directory:
            path = Path(directory)
            built = cls.build(path, documents)
            del built
            files = {
                item.name: base64.b64encode(item.read_bytes()).decode("ascii")
                for item in sorted(path.iterdir())
                if item.is_file() and not item.name.startswith(".") and not item.name.endswith(".lock")
            }
            payload = json.dumps(
                {"format_version": 1, "backend_version": tantivy_capability().version,
                 "documents": json.loads(Bm25fIndex(documents).to_payload()), "files": files},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > MAX_SHARD_PAYLOAD_BYTES:
                raise ValueError("native index payload exceeds shard size limit")
            return payload

    @classmethod
    def from_payload(cls, payload: bytes) -> TantivyIndex:
        package = _native_package(payload)
        require_tantivy()
        if package["backend_version"] != tantivy_capability().version:
            raise ArtifactValidationError("Tantivy backend version mismatch; rebuild the generation")
        temporary = tempfile.TemporaryDirectory(prefix="search-tantivy-reader-")
        try:
            path = Path(temporary.name)
            for name, encoded in package["files"].items():
                (path / name).write_bytes(base64.b64decode(encoded, validate=True))
            result = cls.open(path)
            result._temporary = temporary
            return result
        except (ValueError, RuntimeError, OSError) as exc:
            temporary.cleanup()
            raise ArtifactValidationError("cannot open native index package") from exc

    def close(self) -> None:
        self._index = None
        if self._temporary is not None:
            # Native background threads can briefly retain handles on Windows.
            for attempt in range(8):
                try:
                    self._temporary.cleanup()
                    break
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {32, 145} or attempt == 7:
                        raise
                    time.sleep(0.02)
            self._temporary = None

    def __del__(self) -> None:
        self.close()

    @classmethod
    def build(cls, path: Path, documents: tuple[SearchDocument, ...]) -> TantivyIndex:
        tantivy = _tantivy()
        path.mkdir(parents=True, exist_ok=True)
        builder = tantivy.SchemaBuilder()
        for field in ("url_id", *cls._TEXT_FIELDS):
            builder.add_text_field(field, stored=True)
        builder.add_text_field("crawled_at", stored=True)
        index = tantivy.Index(builder.build(), str(path))
        index.config_reader(reload_policy="Manual")
        writer = index.writer(15_000_000, 1)
        for document in documents:
            host = document.host or _host_from_url(document.url)
            domain = document.domain or host
            writer.add_document(
                tantivy.Document(
                    url_id=document.url_id.hex,
                    url=document.url,
                    title=document.title,
                    body=document.body,
                    headings=document.headings,
                    anchor=document.anchor,
                    host=host,
                    domain=domain,
                    crawled_at=document.crawled_at or "",
                )
            )
        writer.commit()
        index.reload()
        return cls(index)

    @classmethod
    def open(cls, path: Path) -> TantivyIndex:
        index = _tantivy().Index.open(str(path))
        index.config_reader(reload_policy="Manual")
        return cls(index)

    def search(self, query: str, top_k: int) -> tuple[SearchHit, ...]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        from .query_analysis import analyze_query

        analysis = analyze_query(query) if query.strip() else None
        if analysis and analysis.url_intent:
            query = json.dumps(query)
        else:
            query = re.sub(r"(?<!\S)([\w]+(?:\+\+|#))(?=\s|$)", lambda match: json.dumps(match.group(1)), query)
        fuzzy_clauses = []
        if analysis and analysis.fuzzy_terms:
            if any(part in {"AND", "OR", "NOT"} or ":" in part or part.startswith("-") for part in query.split()):
                raise ValueError("fuzzy modifiers support simple terms/phrases, not Boolean or field syntax")
            tantivy = _tantivy()
            for term in analysis.fuzzy_terms:
                for field, boost in zip(self._TEXT_FIELDS, (3.0, 1.0, 2.0, 1.5, 1.25, 1.2, 1.1), strict=True):
                    fuzzy = tantivy.Query.fuzzy_term_query(self._index.schema, field, term, distance=1, transposition_cost_one=True)
                    fuzzy_clauses.append((tantivy.Occur.Should, tantivy.Query.boost_query(fuzzy, boost)))
            query = " ".join(part for part in query.split() if part.casefold().rstrip("~") not in analysis.fuzzy_terms)
        tantivy_query = self._index.parse_query(
            query,
            list(self._TEXT_FIELDS),
            field_boosts={
                "title": 3.0,
                "headings": 2.0,
                "url": 1.5,
                "anchor": 1.25,
                "host": 1.2,
                "domain": 1.1,
                "body": 1.0,
            },
        ) if query.strip() else (_tantivy().Query.empty_query() if fuzzy_clauses else _tantivy().Query.all_query())
        if fuzzy_clauses:
            fuzzy_clauses.append((_tantivy().Occur.Should, tantivy_query))
            tantivy_query = _tantivy().Query.boolean_query(fuzzy_clauses)
        searcher = self._index.searcher()
        result = searcher.search(tantivy_query, limit=top_k)
        hits: list[SearchHit] = []
        for score, address in result.hits:
            document = searcher.doc(address).to_dict()
            url_id = UrlId(bytes.fromhex(_stored_value(document, "url_id")))
            url = _stored_value(document, "url")
            title = _stored_value(document, "title")
            body = _stored_value(document, "body")
            timestamp = document.get("crawled_at", [""])[0] or None
            hits.append(SearchHit(url_id, float(score), title, url, make_snippet(body, query), timestamp))
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.url_id.hex)))


def _tantivy():
    require_tantivy()
    import tantivy

    return tantivy


def _stored_value(document: dict[str, list[object]], field: str) -> str:
    values = document.get(field)
    if not values or not isinstance(values[0], str):
        raise ValueError(f"Tantivy document is missing stored {field!r}")
    return values[0]


def _host_from_url(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"search document URL has no host: {url!r}")
    return host.casefold()


def native_documents(payload: bytes) -> tuple[SearchDocument, ...]:
    from .index import documents_from_records

    return tuple(documents_from_records(_native_package(payload)["documents"]))


def _native_package(payload: bytes) -> dict:
    if len(payload) > MAX_SHARD_PAYLOAD_BYTES:
        raise ArtifactValidationError("native payload exceeds size limit")
    try:
        package = json.loads(payload)
        if package["format_version"] != 1 or not isinstance(package["files"], dict):
            raise ValueError("invalid native package format")
        if not isinstance(package["backend_version"], str) or not isinstance(package["documents"], list):
            raise TypeError("native package requires backend version and source records")
        if "meta.json" not in package["files"] or len(package["files"]) > 4096:
            raise ValueError("invalid native file inventory")
        for name, encoded in package["files"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) or ":" in name:
                raise ValueError("unsafe native file name")
            if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
                raise ValueError("reserved native file name")
            base64.b64decode(encoded, validate=True)
        return package
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactValidationError("invalid native index package") from exc
