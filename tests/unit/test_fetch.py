import gzip
import ssl
import zlib

import pytest
from search_engine.fetch import FetchResult, SafeHttpProbe, _decode_response_body, _host_header
from search_engine.liveness import DnsResult, DnsStatus

import search_engine.engine.fetch as fetch_module


class _PublicResolver:
    def resolve(self, _hostname: str, _port: int) -> DnsResult:
        return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))


def test_probe_uses_resolved_address_and_follows_safe_redirect() -> None:
    captured: list[object] = []

    def transport(*args):
        captured.extend(args)
        if args[1] == "example.com":
            return FetchResult(302, {"location": "http://next.example/"}, b"", "http://next.example/")
        return FetchResult(200, {}, b"ok")

    result = SafeHttpProbe(_PublicResolver(), transport=transport).probe("https://example.com/path")
    assert captured[2] == "8.8.8.8"
    assert result.status == 200
    assert result.redirect_target == "http://next.example/"
    assert captured[8] == "next.example"


def test_probe_stops_before_transport_for_nonpublic_dns() -> None:
    class PrivateResolver:
        def resolve(self, _hostname: str, _port: int) -> DnsResult:
            return DnsResult(DnsStatus.DISALLOWED_ADDRESS, ("127.0.0.1",))

    assert SafeHttpProbe(PrivateResolver()).probe("http://example.com").error == "dns:disallowed_address"


def test_probe_defensively_rejects_private_addresses_from_a_faulty_resolver() -> None:
    class FaultyResolver:
        def resolve(self, _hostname: str, _port: int) -> DnsResult:
            return DnsResult(DnsStatus.RESOLVABLE, ("127.0.0.1",))

    def transport(*_args):
        raise AssertionError("transport must not receive a private address")

    result = SafeHttpProbe(FaultyResolver(), transport=transport).probe("http://example.com")
    assert result.error == "dns:disallowed_address"


def test_host_header_preserves_non_default_port_and_ipv6_brackets() -> None:
    assert _host_header("example.com", 8080, "http") == "example.com:8080"
    assert _host_header("2001:4860:4860::8888", 8443, "https") == "[2001:4860:4860::8888]:8443"


def test_probe_rechecks_redirect_address_before_transport() -> None:
    class RedirectResolver:
        def resolve(self, hostname: str, _port: int) -> DnsResult:
            if hostname == "private.example":
                return DnsResult(DnsStatus.DISALLOWED_ADDRESS, ("127.0.0.1",))
            return DnsResult(DnsStatus.RESOLVABLE, ("8.8.8.8",))

    def transport(*_args):
        return FetchResult(302, {"location": "http://private.example/"}, b"", "http://private.example/")

    result = SafeHttpProbe(RedirectResolver(), transport=transport).probe("https://example.com/")
    assert result.error == "dns:disallowed_address"


def test_probe_enforces_redirect_limit() -> None:
    def transport(*args):
        host = args[1]
        return FetchResult(302, {"location": f"https://next-{host}/"}, b"", f"https://next-{host}/")

    result = SafeHttpProbe(_PublicResolver(), max_redirects=1, transport=transport).probe("https://example.com/")
    assert result.error == "redirect_limit_exceeded"


def test_probe_rejects_redirect_without_location() -> None:
    result = SafeHttpProbe(
        _PublicResolver(), transport=lambda *_args: FetchResult(302, {}, b"", redirect_target=None)
    ).probe("https://example.com/")
    assert result.error == "redirect_missing_location"


def test_https_transport_reports_tls_failures_separately(monkeypatch) -> None:
    class FailingConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, *_args, **_kwargs) -> None:
            raise ssl.SSLError("certificate failed")

        def close(self) -> None:
            pass

    monkeypatch.setattr(fetch_module, "_PinnedHttpsConnection", FailingConnection)
    result = fetch_module._request_once("https", "example.com", "8.8.8.8", 443, "/", 1.0, 1_024)
    assert (result.tls_status, result.error) == ("invalid", "tls:SSLError")


def test_bounded_response_decompression_handles_supported_encodings() -> None:
    assert _decode_response_body(gzip.compress(b"search body"), "gzip", 100) == b"search body"
    assert _decode_response_body(zlib.compress(b"search body"), "deflate", 100) == b"search body"
    with pytest.raises(ValueError, match="limit"):
        _decode_response_body(gzip.compress(b"x" * 101), "gzip", 100)


def test_http_transport_reports_status_and_rejects_body_over_limit(monkeypatch) -> None:
    class Response:
        status = 503

        def getheaders(self):
            return [("Content-Type", "text/html")]

        def read(self, _size: int) -> bytes:
            return b"unavailable"

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, *_args, **_kwargs) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            raise OSError("already closed")

    monkeypatch.setattr(fetch_module.http.client, "HTTPConnection", Connection)
    result = fetch_module._request_once("http", "example.com", "8.8.8.8", 80, "/", 1.0, 16)
    assert (result.status, result.headers["content-type"], result.body) == (503, "text/html", b"unavailable")
