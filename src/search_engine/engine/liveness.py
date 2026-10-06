"""DNS outcomes, address safety, and deterministic liveness classification."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class DnsStatus(StrEnum):
    RESOLVABLE = "resolvable"
    NXDOMAIN = "nxdomain"
    TEMPORARY_FAILURE = "temporary_failure"
    DISALLOWED_ADDRESS = "disallowed_address"
    UNKNOWN = "unknown"


class LivenessClassification(StrEnum):
    LIVE = "live"
    LIVE_BUT_BLOCKED = "live_but_blocked"
    LIVE_BUT_UNUSABLE = "live_but_unusable"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DnsResult:
    status: DnsStatus
    addresses: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CachedDnsResult:
    result: DnsResult
    expires_at: float


class DnsResolver:
    """Resolve public addresses only by default, suitable for crawler/probe use."""

    def __init__(
        self,
        *,
        positive_ttl_seconds: float = 300.0,
        error_ttl_seconds: float = 30.0,
        max_cache_entries: int = 4_096,
        lookup: Callable[..., list[tuple[object, ...]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if positive_ttl_seconds <= 0 or error_ttl_seconds <= 0 or max_cache_entries <= 0:
            raise ValueError("DNS cache TTLs and max_cache_entries must be positive")
        self.positive_ttl_seconds = positive_ttl_seconds
        self.error_ttl_seconds = error_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.lookup = lookup or socket.getaddrinfo
        self.clock = clock
        self._cache: dict[tuple[str, int], _CachedDnsResult] = {}
        self._cache_lock = threading.Lock()

    def resolve(self, hostname: str, port: int) -> DnsResult:
        normalized_host = hostname.rstrip(".").casefold()
        if not normalized_host or not 1 <= port <= 65535:
            return DnsResult(DnsStatus.UNKNOWN, error="invalid hostname or port")
        cache_key = (normalized_host, port)
        now = self.clock()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > now:
                return cached.result
            self._cache.pop(cache_key, None)
        try:
            answers = self.lookup(normalized_host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            nxdomain_errors = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
            status = DnsStatus.NXDOMAIN if exc.errno in nxdomain_errors else DnsStatus.TEMPORARY_FAILURE
            result = DnsResult(status, error=str(exc))
        except TimeoutError as exc:
            result = DnsResult(DnsStatus.TEMPORARY_FAILURE, error=str(exc) or "DNS lookup timed out")
        except OSError as exc:
            result = DnsResult(DnsStatus.UNKNOWN, error=str(exc))
        else:
            addresses = tuple(sorted({str(answer[4][0]) for answer in answers}))
            if not addresses:
                result = DnsResult(DnsStatus.UNKNOWN, error="no addresses returned")
            elif any(not is_public_address(address) for address in addresses):
                result = DnsResult(DnsStatus.DISALLOWED_ADDRESS, addresses, "non-public address resolved")
            else:
                result = DnsResult(DnsStatus.RESOLVABLE, addresses)
        self._store(cache_key, result, now)
        return result

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _store(self, key: tuple[str, int], result: DnsResult, now: float) -> None:
        ttl = self.positive_ttl_seconds if result.status is DnsStatus.RESOLVABLE else self.error_ttl_seconds
        with self._cache_lock:
            while len(self._cache) >= self.max_cache_entries:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = _CachedDnsResult(result, now + ttl)


def is_public_address(address: str) -> bool:
    """Reject private, loopback, link-local, multicast, reserved, and unspecified IPs."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global


def classify_liveness(dns_status: DnsStatus, http_status: int | None) -> LivenessClassification:
    if dns_status is DnsStatus.NXDOMAIN:
        return LivenessClassification.DEAD
    if dns_status in {DnsStatus.TEMPORARY_FAILURE, DnsStatus.UNKNOWN}:
        return LivenessClassification.TEMPORARILY_UNAVAILABLE
    if dns_status is DnsStatus.DISALLOWED_ADDRESS:
        return LivenessClassification.LIVE_BUT_UNUSABLE
    if http_status is None:
        return LivenessClassification.UNKNOWN
    if http_status in {401, 403, 429}:
        return LivenessClassification.LIVE_BUT_BLOCKED
    if 200 <= http_status < 400:
        return LivenessClassification.LIVE
    if 500 <= http_status < 600:
        return LivenessClassification.TEMPORARILY_UNAVAILABLE
    if http_status in {404, 410}:
        return LivenessClassification.DEAD
    return LivenessClassification.LIVE_BUT_UNUSABLE
