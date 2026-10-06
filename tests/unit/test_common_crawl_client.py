from io import BytesIO

from search_engine.common_crawl import CommonCrawlIndexClient


class _Response(BytesIO):
    def close(self) -> None:
        super().close()


class _PartialResponse:
    def __init__(self, lines: list[bytes], *, fail_after: int | None = None) -> None:
        self.lines = iter(lines)
        self.remaining_before_failure = fail_after

    def readline(self, _limit: int) -> bytes:
        if self.remaining_before_failure == 0:
            raise OSError("temporary connection failure")
        if self.remaining_before_failure is not None:
            self.remaining_before_failure -= 1
        return next(self.lines, b"")

    def close(self) -> None:
        pass


def test_common_crawl_client_streams_index_lines_and_encodes_query() -> None:
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response(b'{"url":"https://example.com/"}\n')

    lines = list(CommonCrawlIndexClient(opener=opener).stream_index("CC-MAIN-2026-01", "example.com/*"))
    assert lines == ['{"url":"https://example.com/"}']
    assert "matchType=domain" in captured["url"]


def test_common_crawl_client_lists_bounded_collection_metadata() -> None:
    def opener(_request, *, timeout):
        assert timeout == 30.0
        return _Response(b'[{"id":"CC-MAIN-2026-01","name":"January","cdx-api":"https://index.example/"}]')

    collections = CommonCrawlIndexClient(opener=opener).list_collections()
    assert collections[0].crawl_id == "CC-MAIN-2026-01"


def test_common_crawl_client_retries_partial_stream_without_duplicate_lines() -> None:
    responses = iter(
        (
            _PartialResponse([b'{"url":"https://one.example/"}\n'], fail_after=1),
            _PartialResponse([b'{"url":"https://one.example/"}\n', b'{"url":"https://two.example/"}\n']),
        )
    )
    delays: list[float] = []

    def opener(_request, *, timeout):
        assert timeout == 30.0
        return next(responses)

    client = CommonCrawlIndexClient(opener=opener, retry_delay_seconds=0.25, sleeper=delays.append)
    assert list(client.stream_index("CC-MAIN-2026-01", "example.com/*")) == [
        '{"url":"https://one.example/"}',
        '{"url":"https://two.example/"}',
    ]
    assert delays == [0.25]


def test_common_crawl_client_stops_before_opening_when_cancelled() -> None:
    def opener(_request, *, timeout):
        raise AssertionError("cancelled stream must not open a request")

    client = CommonCrawlIndexClient(opener=opener)
    assert list(client.stream_index("CC-MAIN-2026-01", "example.com/*", cancelled=lambda: True)) == []
