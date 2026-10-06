import pytest
from search_engine.canonicalization import canonicalize_url
from search_engine.errors import UrlCanonicalizationError
from search_engine.identity import UrlId


@pytest.mark.parametrize(("raw", "expected"), [
    ("HTTP://BÜCHER.example:80/a/./b/../%7e?q=%7e%2f#fragment", "http://xn--bcher-kva.example/a/~?q=~%2F"),
    ("https://Example.COM", "https://example.com/"),
    ("https://example.com//a///b/", "https://example.com/a/b"),
    ("https://[2001:4860:4860::8888]/", "https://[2001:4860:4860::8888]/"),
])
def test_canonicalization(raw: str, expected: str) -> None:
    canonical = canonicalize_url(raw)
    assert canonical.value == expected
    assert canonicalize_url(canonical.value) == canonical


@pytest.mark.parametrize("raw", ["ftp://example.com", "https://u:p@example.com", "https://", "https://exa mple.com", "https://[::1", "https://example.com/%", "https://example.com/%2", "https://example.com/%zz", "https://example.com:0/"])
def test_rejects_unsupported_or_unsafe_url(raw: str) -> None:
    with pytest.raises(UrlCanonicalizationError):
        canonicalize_url(raw)


def test_url_id_is_deterministic_and_128_bit() -> None:
    canonical = canonicalize_url("https://example.com/")
    assert UrlId.from_canonical_url(canonical) == UrlId.from_canonical_url(canonical)
    assert len(UrlId.from_canonical_url(canonical).value) == 16
