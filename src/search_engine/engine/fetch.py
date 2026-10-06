"""Bounded HTTP(S) probing pinned to DNS-validated public addresses."""

from __future__ import annotations

import gzip
import http.client
import io
import socket
import ssl
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

from .canonicalization import UrlCanonicalizationError, canonicalize_url
from .liveness import DnsResolver, DnsStatus, is_public_address


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int | None
    headers: dict[str, str]
    body: bytes
    redirect_target: str | None = None
    tls_status: str | None = None
    error: str | None = None


RequestTransport = Callable[[str, str, str, int, str, float, int], FetchResult]


class SafeHttpProbe:
    """Probe an HTTP URL without allowing its hostname to reach private networks.

    Connections use DNS-validated numeric addresses. Redirect targets are
    canonicalized and re-resolved before every hop, preventing redirects from
    bypassing the address-safety policy.
    """

    def __init__(
        self,
        resolver: DnsResolver | None = None,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 16 * 1024,
        max_redirects: int = 3,
        transport: RequestTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("timeout_seconds/max_response_bytes must be positive and max_redirects non-negative")
        self.resolver = resolver or DnsResolver()
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.transport = transport or _request_once

    def probe(self, raw_url: str) -> FetchResult:
        canonical = canonicalize_url(raw_url)
        current_url = canonical.value
        visited = {current_url}
        final_redirect_target: str | None = None
        for redirects_followed in range(self.max_redirects + 1):
            result = self._probe_once(current_url)
            if not _is_redirect(result):
                return replace(result, redirect_target=final_redirect_target) if final_redirect_target else result
            if not result.redirect_target:
                return FetchResult(None, result.headers, b"", error="redirect_missing_location")
            try:
                target = canonicalize_url(urljoin(current_url, result.redirect_target)).value
            except UrlCanonicalizationError:
                return FetchResult(None, result.headers, b"", redirect_target=result.redirect_target, error="invalid_redirect")
            if target in visited:
                return FetchResult(None, result.headers, b"", redirect_target=target, error="redirect_loop")
            if redirects_followed == self.max_redirects:
                return FetchResult(None, result.headers, b"", redirect_target=target, error="redirect_limit_exceeded")
            visited.add(target)
            final_redirect_target = target
            current_url = target
        raise AssertionError("redirect loop must return before exhaustion")

    def _probe_once(self, canonical_url: str) -> FetchResult:
        parts = urlsplit(canonical_url)
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        dns = self.resolver.resolve(parts.hostname or "", port)
        if dns.status is not DnsStatus.RESOLVABLE:
            return FetchResult(None, {}, b"", error=f"dns:{dns.status}")
        if not dns.addresses or any(not is_public_address(address) for address in dns.addresses):
            return FetchResult(None, {}, b"", error="dns:disallowed_address")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        # DnsResolver guarantees every returned address is public. Pick one;
        # retries/failover are scheduled by the liveness/frontier layer.
        return self.transport(
            parts.scheme,
            parts.hostname or "",
            dns.addresses[0],
            port,
            path,
            self.timeout_seconds,
            self.max_response_bytes,
        )


def _is_redirect(result: FetchResult) -> bool:
    return result.status is not None and 300 <= result.status < 400


def _request_once(
    scheme: str,
    hostname: str,
    address: str,
    port: int,
    path: str,
    timeout_seconds: float,
    max_response_bytes: int,
) -> FetchResult:
    try:
        if scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHttpsConnection(
                address, port, hostname, timeout_seconds
            )
        else:
            connection = http.client.HTTPConnection(address, port, timeout=timeout_seconds)
        connection.request(
            "GET",
            path,
            headers={
                "Host": _host_header(hostname, port, scheme),
                "User-Agent": "OpenSearchLocalBot/0.1",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "identity",
                "Range": f"bytes=0-{max_response_bytes - 1}",
            },
        )
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            return FetchResult(None, {}, b"", error="response exceeds byte limit")
        try:
            body = _decode_response_body(body, headers.get("content-encoding"), max_response_bytes)
        except (OSError, ValueError, zlib.error):
            return FetchResult(None, headers, b"", error="invalid_or_oversized_compressed_response")
        redirect = headers.get("location") if 300 <= response.status < 400 else None
        tls_status = "valid" if scheme == "https" else None
        return FetchResult(response.status, headers, body, redirect, tls_status)
    except ssl.SSLError as exc:
        return FetchResult(None, {}, b"", tls_status="invalid", error=f"tls:{type(exc).__name__}")
    except (http.client.HTTPException, OSError) as exc:
        return FetchResult(None, {}, b"", error=f"network:{type(exc).__name__}")
    finally:
        if "connection" in locals():
            try:
                connection.close()
            except OSError:
                pass


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, address: str, port: int, hostname: str, timeout: float) -> None:
        super().__init__(address, port=port, timeout=timeout, context=ssl.create_default_context())
        self._tls_hostname = hostname

    def connect(self) -> None:
        socket_connection = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(socket_connection, server_hostname=self._tls_hostname)


def _host_header(hostname: str, port: int, scheme: str) -> str:
    authority = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return authority if port == default_port else f"{authority}:{port}"


def _decode_response_body(body: bytes, encoding: str | None, maximum_bytes: int) -> bytes:
    if encoding is None or encoding.casefold().strip() in {"", "identity"}:
        return body
    normalized = encoding.casefold().strip()
    if normalized in {"gzip", "x-gzip"}:
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as stream:
            decoded = stream.read(maximum_bytes + 1)
    elif normalized == "deflate":
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(body, maximum_bytes + 1)
        decoded += decompressor.flush(maximum_bytes + 1 - len(decoded))
    else:
        raise ValueError("unsupported content encoding")
    if len(decoded) > maximum_bytes:
        raise ValueError("decompressed response exceeds byte limit")
    return decoded
