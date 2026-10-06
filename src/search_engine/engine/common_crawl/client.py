"""Bounded streaming client for Common Crawl CDX JSON index responses."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import BinaryIO
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class CommonCrawlCollection:
    crawl_id: str
    name: str
    index_url: str


class CommonCrawlIndexClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_line_bytes: int = 1 << 20,
        max_metadata_bytes: int = 1 << 20,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        opener: Callable[..., BinaryIO] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or max_line_bytes <= 0 or max_metadata_bytes <= 0 or max_attempts <= 0:
            raise ValueError("timeout_seconds, byte limits, and max_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        self.timeout_seconds = timeout_seconds
        self.max_line_bytes = max_line_bytes
        self.max_metadata_bytes = max_metadata_bytes
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.opener = opener
        self.sleeper = sleeper

    def list_collections(self) -> tuple[CommonCrawlCollection, ...]:
        request = Request("https://index.commoncrawl.org/collinfo.json", headers={"User-Agent": "OpenSearchLocalBot/0.1"})
        with closing(self.opener(request, timeout=self.timeout_seconds)) as response:
            payload = response.read(self.max_metadata_bytes + 1)
        if len(payload) > self.max_metadata_bytes:
            raise ValueError("Common Crawl metadata exceeds configured limit")
        try:
            records = json.loads(payload)
            collections = tuple(
                CommonCrawlCollection(str(record["id"]), str(record.get("name", "")), str(record["cdx-api"]))
                for record in records
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid Common Crawl collection metadata") from exc
        if not collections or any(not item.crawl_id or not item.index_url for item in collections):
            raise ValueError("invalid Common Crawl collection metadata")
        return collections

    def stream_index(
        self,
        crawl_id: str,
        url_pattern: str,
        *,
        match_type: str = "domain",
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        if not crawl_id or not url_pattern:
            raise ValueError("crawl_id and url_pattern are required")
        if match_type not in {"domain", "prefix", "host", "exact"}:
            raise ValueError("unsupported Common Crawl match type")
        endpoint = f"https://index.commoncrawl.org/{crawl_id}-index?{urlencode({'url': url_pattern, 'matchType': match_type, 'output': 'json'})}"
        request = Request(endpoint, headers={"User-Agent": "OpenSearchLocalBot/0.1"})
        emitted = 0
        for attempt in range(1, self.max_attempts + 1):
            if cancelled and cancelled():
                return
            try:
                with closing(self.opener(request, timeout=self.timeout_seconds)) as response:
                    skipped = 0
                    while line := response.readline(self.max_line_bytes + 1):
                        if cancelled and cancelled():
                            return
                        if len(line) > self.max_line_bytes:
                            raise ValueError("Common Crawl index line exceeds configured limit")
                        if skipped < emitted:
                            skipped += 1
                            continue
                        emitted += 1
                        yield line.decode("utf-8", errors="strict").rstrip("\r\n")
                return
            except (OSError, TimeoutError, URLError):
                if attempt == self.max_attempts:
                    raise
                if cancelled and cancelled():
                    return
                self.sleeper(self.retry_delay_seconds * attempt)
