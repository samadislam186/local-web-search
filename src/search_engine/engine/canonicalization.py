"""Versioned canonical URL representation used by every discovery source."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .errors import UrlCanonicalizationError

CANONICALIZATION_VERSION = 1
_SUPPORTED_SCHEMES = frozenset({"http", "https"})
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")


@dataclass(frozen=True, slots=True)
class CanonicalUrl:
    value: str
    version: int = CANONICALIZATION_VERSION


def canonicalize_url(raw_url: str) -> CanonicalUrl:
    """Return canonical HTTP(S) identity without guessing away query semantics."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise UrlCanonicalizationError("URL must be a non-empty string")
    if any(character.isspace() for character in raw_url):
        raise UrlCanonicalizationError("URL must not contain whitespace")
    try:
        parts = urlsplit(raw_url)
    except ValueError as exc:
        raise UrlCanonicalizationError("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise UrlCanonicalizationError(f"unsupported URL scheme: {parts.scheme or '<missing>'}")
    if parts.username is not None or parts.password is not None:
        raise UrlCanonicalizationError("URL credentials are not permitted")
    if not parts.hostname:
        raise UrlCanonicalizationError("URL hostname is required")
    host = _canonical_host(parts.hostname)
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlCanonicalizationError("invalid URL port") from exc
    if port == 0:
        raise UrlCanonicalizationError("URL port must be between 1 and 65535")
    host_authority = f"[{host}]" if _is_ipv6(host) else host
    netloc = host_authority if port is None or (scheme, port) in {("http", 80), ("https", 443)} else f"{host_authority}:{port}"
    path = _normalize_path(parts.path)
    query = _normalize_percent_encoding(parts.query)
    return CanonicalUrl(urlunsplit(SplitResult(scheme, netloc, path, query, "")))


def _canonical_host(host: str) -> str:
    host = host.rstrip(".").lower()
    if not host:
        raise UrlCanonicalizationError("URL hostname is required")
    try:
        if _is_ipv6(host):
            return str(ipaddress.IPv6Address(host))
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlCanonicalizationError("invalid internationalized hostname") from exc


def _is_ipv6(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


def _normalize_path(path: str) -> str:
    path = _normalize_percent_encoding(path or "/")
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    normalized = "/" + "/".join(segments)
    return normalized if normalized else "/"


def _normalize_percent_encoding(value: str) -> str:
    """Decode only unreserved escapes and uppercase all preserved hex escapes."""
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise UrlCanonicalizationError("malformed percent encoding")

    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        return character if character in _UNRESERVED else "%" + match.group(1).upper()

    return _PERCENT_ESCAPE.sub(replace, value)
