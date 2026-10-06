"""Single-work-item crawler orchestration over the leased frontier."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from .crawl_processing import CrawledDocument, CrawlResultProcessor
from .crawl_store import CrawledDocumentRepository
from .fetch import SafeHttpProbe
from .robots_manager import RobotsDecision
from .state import CandidateRepository, FrontierRepository, UrlState

_CHARSET = re.compile(r"(?:^|;)\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^;\s]+))", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CrawlAttemptResult:
    leased: bool
    completed: bool
    reason: str | None = None
    document: CrawledDocument | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay: timedelta = timedelta(seconds=30)
    maximum_delay: timedelta = timedelta(hours=1)
    max_attempts: int = 5

    def delay_for_attempt(self, attempt_count: int) -> timedelta:
        if (
            attempt_count <= 0
            or self.max_attempts <= 0
            or self.base_delay <= timedelta()
            or self.maximum_delay < self.base_delay
        ):
            raise ValueError("invalid retry policy")
        return min(self.base_delay * (2 ** (attempt_count - 1)), self.maximum_delay)


class CrawlerWorker:
    def __init__(
        self,
        frontier: FrontierRepository,
        candidates: CandidateRepository,
        probe: SafeHttpProbe,
        processor: CrawlResultProcessor,
        *,
        document_store: CrawledDocumentRepository | None = None,
        robots_allows: Callable[[str], bool] | None = None,
        robots_check: Callable[[str], RobotsDecision] | None = None,
        robots_delay: Callable[[str], float | None] | None = None,
        retry_policy: RetryPolicy | None = None,
        maximum_retry_after: timedelta = timedelta(days=1),
    ) -> None:
        self.frontier = frontier
        self.candidates = candidates
        self.probe = probe
        self.processor = processor
        self.document_store = document_store
        self.robots_allows = robots_allows or (lambda _url: True)
        self.robots_check = robots_check
        self.robots_delay = robots_delay
        self.retry_policy = retry_policy or RetryPolicy()
        if maximum_retry_after <= timedelta():
            raise ValueError("maximum_retry_after must be positive")
        self.maximum_retry_after = maximum_retry_after

    def run_once(
        self,
        owner: str,
        *,
        now: str,
        lease_expires_at: str,
        retry_at: str | None,
        next_allowed_at: str,
    ) -> CrawlAttemptResult:
        lease = self.frontier.lease_next_details(owner, now, lease_expires_at)
        if lease is None:
            return CrawlAttemptResult(False, False, "frontier_empty")
        url_id = lease.url_id
        canonical = self.candidates.canonical_url(url_id)
        if self.robots_check is not None:
            try:
                decision = self.robots_check(canonical.value)
            except Exception as exc:  # noqa: BLE001 - worker boundaries convert policy faults into retries.
                return self._retry(url_id, owner, now, retry_at, lease.attempt_count, f"robots:{type(exc).__name__}")
            if decision is RobotsDecision.UNAVAILABLE:
                return self._retry(url_id, owner, now, retry_at, lease.attempt_count, "robots:unavailable")
            allowed = decision is RobotsDecision.ALLOWED
        else:
            allowed = self.robots_allows(canonical.value)
        if not allowed:
            current = self.candidates.get_state(url_id)
            if current is UrlState.CRAWLABLE:
                self.candidates.transition(url_id, UrlState.LIVE_BUT_BLOCKED, "robots_disallow")
            self.frontier.complete(url_id, owner, next_allowed_at, "robots_disallow")
            return CrawlAttemptResult(True, True, "robots_disallow")
        if self.robots_delay is not None:
            delay_seconds = self.robots_delay(canonical.value)
            if delay_seconds is not None:
                if delay_seconds < 0:
                    return self._retry(url_id, owner, now, retry_at, lease.attempt_count, "robots:invalid_crawl_delay")
                next_allowed_at = max(next_allowed_at, _add_delay(now, timedelta(seconds=delay_seconds)))
        try:
            fetched = self.probe.probe(canonical.value)
        except Exception as exc:  # noqa: BLE001 - worker boundaries convert probe faults into retries.
            return self._retry(url_id, owner, now, retry_at, lease.attempt_count, f"probe:{type(exc).__name__}")
        content_type = fetched.headers.get("content-type", "").split(";", 1)[0].lower()
        if fetched.status is None or not 200 <= fetched.status < 300:
            reason = fetched.error or f"http_status:{fetched.status}"
            return self._retry(
                url_id,
                owner,
                now,
                retry_at,
                lease.attempt_count,
                reason,
                next_allowed_at=_next_allowed_at(
                    next_allowed_at,
                    fetched.headers.get("retry-after"),
                    now,
                    maximum_delay=self.maximum_retry_after,
                ),
            )
        if content_type not in {"text/html", "application/xhtml+xml"}:
            reason = f"unsupported_content_type:{content_type}"
            current = self.candidates.get_state(url_id)
            if current in {UrlState.LIVE, UrlState.CRAWLABLE}:
                self.candidates.transition(url_id, UrlState.LIVE_BUT_UNUSABLE, reason)
            self.frontier.complete(url_id, owner, next_allowed_at, reason)
            return CrawlAttemptResult(True, True, reason)
        try:
            body = _decode_html_body(fetched.body, fetched.headers.get("content-type", ""))
            if fetched.redirect_target is None:
                document = self.processor.process_html(url_id, body)
            else:
                document = self.processor.process_html(url_id, body, redirect_target=fetched.redirect_target)
            if self.document_store is not None:
                self.document_store.save(document)
        except Exception as exc:  # noqa: BLE001 - worker boundaries convert processing faults into retries.
            return self._retry(url_id, owner, now, retry_at, lease.attempt_count, f"processing:{type(exc).__name__}")
        self.frontier.complete(url_id, owner, next_allowed_at)
        return CrawlAttemptResult(True, True, document=document)

    def _retry(
        self,
        url_id,
        owner: str,
        now: str,
        retry_at: str | None,
        attempt_count: int,
        reason: str,
        *,
        next_allowed_at: str | None = None,
    ) -> CrawlAttemptResult:
        if attempt_count >= self.retry_policy.max_attempts:
            current = self.candidates.get_state(url_id)
            if current in {UrlState.LIVE, UrlState.CRAWLABLE}:
                self.candidates.transition(url_id, UrlState.TEMPORARILY_UNAVAILABLE, f"retry_exhausted:{reason}")
            exhausted_reason = f"retry_exhausted:{reason}"
            self.frontier.complete(url_id, owner, next_allowed_at or now, exhausted_reason)
            return CrawlAttemptResult(True, True, exhausted_reason)
        scheduled_retry = retry_at or _add_delay(now, self.retry_policy.delay_for_attempt(attempt_count))
        self.frontier.release_for_retry(url_id, owner, scheduled_retry, reason, next_allowed_at=next_allowed_at)
        return CrawlAttemptResult(True, False, reason)


def _add_delay(timestamp: str, delay: timedelta) -> str:
    return (datetime.fromisoformat(timestamp) + delay).isoformat()


def _next_allowed_at(
    default: str, retry_after: str | None, now: str, *, maximum_delay: timedelta = timedelta(days=1)
) -> str:
    """Return the later of crawler politeness and a valid HTTP Retry-After value."""
    if maximum_delay <= timedelta():
        raise ValueError("maximum_delay must be positive")
    if not retry_after:
        return default
    try:
        if retry_after.isdigit():
            candidate = datetime.fromisoformat(now) + timedelta(seconds=int(retry_after))
        else:
            candidate = parsedate_to_datetime(retry_after)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=UTC)
    except (OverflowError, TypeError, ValueError):
        return default
    maximum = datetime.fromisoformat(now) + maximum_delay
    return max(default, min(candidate, maximum).isoformat())


def _decode_html_body(body: bytes, content_type: str) -> str:
    match = _CHARSET.search(content_type)
    charset = next(iter(value for value in match.groups() if value), "utf-8") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
