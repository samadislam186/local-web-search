"""Generation-local graph ordinals and safe hyperlink graph payloads."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .identity import UrlId


@dataclass(frozen=True, slots=True)
class OrdinalMap:
    """Keeps 128-bit UrlIds out of every persisted edge."""

    url_ids: tuple[UrlId, ...]
    _indexes: dict[UrlId, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(set(self.url_ids)) != len(self.url_ids):
            raise ValueError("duplicate UrlId in ordinal map")
        object.__setattr__(self, "_indexes", {url_id: ordinal for ordinal, url_id in enumerate(self.url_ids)})

    @classmethod
    def build(cls, url_ids: list[UrlId]) -> OrdinalMap:
        unique = tuple(sorted(set(url_ids), key=lambda value: value.hex))
        return cls(unique)

    def ordinal_of(self, url_id: UrlId) -> int:
        try:
            return self._indexes[url_id]
        except KeyError as exc:
            raise KeyError(f"UrlId not present in generation: {url_id.hex}") from exc


@dataclass(frozen=True, slots=True)
class HyperlinkGraph:
    ordinals: OrdinalMap
    edges: tuple[tuple[int, int], ...]
    _outgoing: tuple[tuple[int, ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        node_count = len(self.ordinals.url_ids)
        if any(source < 0 or target < 0 or source >= node_count or target >= node_count for source, target in self.edges):
            raise ValueError("invalid graph ordinal references")
        if tuple(sorted(set(self.edges))) != self.edges:
            raise ValueError("graph edges must be sorted and unique")
        adjacency: list[list[int]] = [[] for _ in range(node_count)]
        for source, target in self.edges:
            adjacency[source].append(target)
        object.__setattr__(self, "_outgoing", tuple(tuple(targets) for targets in adjacency))

    @classmethod
    def from_url_edges(cls, edges: list[tuple[UrlId, UrlId]], *, nodes: list[UrlId] | None = None) -> HyperlinkGraph:
        return cls.from_url_edge_batches((edges,), nodes=nodes or ())

    @classmethod
    def from_url_edge_batches(
        cls,
        edge_batches: Iterable[Iterable[tuple[UrlId, UrlId]]],
        *,
        nodes: Iterable[UrlId] = (),
    ) -> HyperlinkGraph:
        """Build an ordinal graph while consuming caller-provided edge batches lazily."""
        url_ids = set(nodes)
        edges: set[tuple[UrlId, UrlId]] = set()
        for batch in edge_batches:
            for source, target in batch:
                if not isinstance(source, UrlId) or not isinstance(target, UrlId):
                    raise TypeError("graph edges must contain UrlIds")
                url_ids.update((source, target))
                edges.add((source, target))
        ordinals = OrdinalMap.build(list(url_ids))
        mapping = {url_id: ordinal for ordinal, url_id in enumerate(ordinals.url_ids)}
        compact = tuple(sorted((mapping[source], mapping[target]) for source, target in edges))
        return cls(ordinals, compact)

    def outgoing(self, source: int) -> tuple[int, ...]:
        return self._outgoing[source]

    def to_payload(self) -> bytes:
        payload = {
            "url_ids": [url_id.hex for url_id in self.ordinals.url_ids],
            "edges": self.edges,
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def iter_payload_chunks(self, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Serialize the compact graph in bounded UTF-8 chunks for shard publication."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        buffer = bytearray()

        def append(value: bytes) -> Iterator[bytes]:
            buffer.extend(value)
            while len(buffer) >= chunk_size:
                yield bytes(buffer[:chunk_size])
                del buffer[:chunk_size]

        yield from append(b'{"url_ids":[')
        for ordinal, url_id in enumerate(self.ordinals.url_ids):
            if ordinal:
                yield from append(b",")
            yield from append(json.dumps(url_id.hex).encode("utf-8"))
        yield from append(b'],"edges":[')
        for ordinal, edge in enumerate(self.edges):
            if ordinal:
                yield from append(b",")
            yield from append(f"[{edge[0]},{edge[1]}]".encode("ascii"))
        yield from append(b"]}")
        if buffer:
            yield bytes(buffer)

    @classmethod
    def from_payload(cls, payload: bytes) -> HyperlinkGraph:
        try:
            data = json.loads(payload)
            ordinals = OrdinalMap(tuple(UrlId(bytes.fromhex(value)) for value in data["url_ids"]))
            edges = tuple((int(source), int(target)) for source, target in data["edges"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid graph payload") from exc
        if tuple(sorted(set(edges))) != edges:
            raise ValueError("graph edges must be sorted and unique")
        return cls(ordinals, edges)


@dataclass(frozen=True, slots=True)
class LogicalEdge:
    """An aggregated logical edge retaining the count needed for later weighting."""

    source: str
    target: str
    count: int


@dataclass(frozen=True, slots=True)
class LogicalGraph:
    """A physical-format-independent directed graph for hosts or registrable domains."""

    nodes: tuple[str, ...]
    edges: tuple[LogicalEdge, ...]


@dataclass(frozen=True, slots=True)
class HostDomainGraphs:
    hosts: LogicalGraph
    domains: LogicalGraph


def derive_host_domain_graphs(
    graph: HyperlinkGraph,
    canonical_urls: dict[UrlId, str],
    *,
    domain_for_host,
) -> HostDomainGraphs:
    """Aggregate URL edges into host and caller-defined registrable-domain layers.

    Public-suffix policy is deployment data, not a string-splitting heuristic;
    callers therefore provide ``domain_for_host`` explicitly.
    """
    hosts = {url_id: _host_for_url(canonical_urls[url_id]) for url_id in graph.ordinals.url_ids}
    domains = {url_id: domain_for_host(host) for url_id, host in hosts.items()}
    if any(not isinstance(domain, str) or not domain for domain in domains.values()):
        raise ValueError("domain_for_host must return non-empty domain strings")
    return HostDomainGraphs(
        _logical_graph(graph, hosts),
        _logical_graph(graph, domains),
    )


def _host_for_url(canonical_url: str) -> str:
    host = urlsplit(canonical_url).hostname
    if not host:
        raise ValueError(f"canonical URL has no host: {canonical_url!r}")
    return host.casefold()


def _logical_graph(graph: HyperlinkGraph, labels: dict[UrlId, str]) -> LogicalGraph:
    counts: dict[tuple[str, str], int] = {}
    for source, target in graph.edges:
        key = (labels[graph.ordinals.url_ids[source]], labels[graph.ordinals.url_ids[target]])
        counts[key] = counts.get(key, 0) + 1
    return LogicalGraph(
        tuple(sorted(set(labels.values()))),
        tuple(LogicalEdge(source, target, count) for (source, target), count in sorted(counts.items())),
    )
