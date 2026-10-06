"""Optional Tantivy capability boundary for the project-owned index contract."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from .identity import UrlId
from .index import SearchDocument, SearchHit
from .snippets import make_snippet


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

    @classmethod
    def build(cls, path: Path, documents: tuple[SearchDocument, ...]) -> TantivyIndex:
        tantivy = _tantivy()
        path.mkdir(parents=True, exist_ok=True)
        builder = tantivy.SchemaBuilder()
        for field in ("url_id", *cls._TEXT_FIELDS):
            builder.add_text_field(field, stored=True)
        index = tantivy.Index(builder.build(), str(path))
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
                )
            )
        writer.commit()
        index.reload()
        return cls(index)

    @classmethod
    def open(cls, path: Path) -> TantivyIndex:
        return cls(_tantivy().Index.open(str(path)))

    def search(self, query: str, top_k: int) -> tuple[SearchHit, ...]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
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
        )
        searcher = self._index.searcher()
        result = searcher.search(tantivy_query, limit=top_k)
        hits: list[SearchHit] = []
        for score, address in result.hits:
            document = searcher.doc(address).to_dict()
            url_id = UrlId(bytes.fromhex(_stored_value(document, "url_id")))
            url = _stored_value(document, "url")
            title = _stored_value(document, "title")
            body = _stored_value(document, "body")
            hits.append(SearchHit(url_id, float(score), title, url, make_snippet(body, query)))
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
