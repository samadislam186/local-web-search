import socket

from search_engine.liveness import (
    DnsResolver,
    DnsStatus,
    LivenessClassification,
    classify_liveness,
    is_public_address,
)


def test_private_addresses_are_not_crawlable() -> None:
    assert not is_public_address("127.0.0.1")
    assert not is_public_address("10.0.0.1")
    assert not is_public_address("169.254.169.254")
    assert not is_public_address("::1")


def test_resolver_rejects_mixed_public_and_private_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    assert DnsResolver().resolve("example.com", 443).status is DnsStatus.DISALLOWED_ADDRESS


def test_liveness_classifier_does_not_make_timeout_permanent_death() -> None:
    assert classify_liveness(DnsStatus.TEMPORARY_FAILURE, None) is LivenessClassification.TEMPORARILY_UNAVAILABLE
    assert classify_liveness(DnsStatus.RESOLVABLE, 403) is LivenessClassification.LIVE_BUT_BLOCKED
    assert classify_liveness(DnsStatus.RESOLVABLE, 200) is LivenessClassification.LIVE


def test_resolver_caches_success_until_its_ttl_expires() -> None:
    clock = [100.0]
    calls = 0

    def lookup(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    resolver = DnsResolver(positive_ttl_seconds=10, lookup=lookup, clock=lambda: clock[0])
    assert resolver.resolve("Example.COM.", 443).status is DnsStatus.RESOLVABLE
    assert resolver.resolve("example.com", 443).status is DnsStatus.RESOLVABLE
    assert calls == 1
    clock[0] += 10
    resolver.resolve("example.com", 443)
    assert calls == 2


def test_resolver_caches_dns_errors_with_shorter_ttl_and_classifies_timeout() -> None:
    clock = [1.0]
    calls = 0

    def lookup(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError()

    resolver = DnsResolver(error_ttl_seconds=2, lookup=lookup, clock=lambda: clock[0])
    assert resolver.resolve("example.com", 443).status is DnsStatus.TEMPORARY_FAILURE
    assert resolver.resolve("example.com", 443).status is DnsStatus.TEMPORARY_FAILURE
    assert calls == 1
    clock[0] += 2
    resolver.resolve("example.com", 443)
    assert calls == 2


def test_resolver_classifies_no_data_as_non_retryable_nxdomain_when_supported() -> None:
    no_data = getattr(socket, "EAI_NODATA", socket.EAI_NONAME)

    def lookup(*_args, **_kwargs):
        raise socket.gaierror(no_data, "no records")

    assert DnsResolver(lookup=lookup).resolve("missing.example", 443).status is DnsStatus.NXDOMAIN
