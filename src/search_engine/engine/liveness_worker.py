"""Lifecycle-aware DNS/HTTP liveness checks for individual candidate URLs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .fetch import SafeHttpProbe
from .liveness import DnsResolver, DnsStatus, LivenessClassification, classify_liveness
from .state import CandidateRepository, LivenessRepository, UrlState, can_transition

_STATE_FOR_CLASSIFICATION = {
    LivenessClassification.LIVE: UrlState.LIVE,
    LivenessClassification.LIVE_BUT_BLOCKED: UrlState.LIVE_BUT_BLOCKED,
    LivenessClassification.LIVE_BUT_UNUSABLE: UrlState.LIVE_BUT_UNUSABLE,
    LivenessClassification.TEMPORARILY_UNAVAILABLE: UrlState.TEMPORARILY_UNAVAILABLE,
    LivenessClassification.DEAD: UrlState.DEAD,
    LivenessClassification.UNKNOWN: UrlState.UNKNOWN,
}


class LivenessWorker:
    def __init__(
        self,
        candidates: CandidateRepository,
        liveness: LivenessRepository,
        *,
        resolver: DnsResolver | None = None,
        probe: SafeHttpProbe | None = None,
        retry_delay: timedelta = timedelta(minutes=5),
        maximum_retry_delay: timedelta = timedelta(hours=1),
    ) -> None:
        self.candidates = candidates
        self.liveness = liveness
        self.resolver = resolver or DnsResolver()
        self.probe = probe or SafeHttpProbe(self.resolver)
        if retry_delay <= timedelta() or maximum_retry_delay < retry_delay:
            raise ValueError("retry delays must be positive and ordered")
        self.retry_delay = retry_delay
        self.maximum_retry_delay = maximum_retry_delay

    def check(self, url_id, *, now: datetime | None = None) -> LivenessClassification:
        checked_at = now or datetime.now(UTC)
        canonical = self.candidates.canonical_url(url_id)
        parts = urlsplit(canonical.value)
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if can_transition(self.candidates.get_state(url_id), UrlState.LIVENESS_PENDING):
            self.candidates.transition(url_id, UrlState.LIVENESS_PENDING)
        dns = self.resolver.resolve(parts.hostname or "", port)
        self.liveness.record_dns(url_id, dns.status, dns.error, recorded_at=checked_at.isoformat())
        if dns.status is not DnsStatus.RESOLVABLE:
            return self._record_and_transition(
                url_id, classify_liveness(dns.status, None), None, dns.error, checked_at=checked_at
            )
        fetched = self.probe.probe(canonical.value)
        classification = classify_liveness(dns.status, fetched.status)
        return self._record_and_transition(
            url_id,
            classification,
            fetched.status,
            fetched.error,
            tls_status=fetched.tls_status,
            redirect_target=fetched.redirect_target,
            checked_at=checked_at,
        )

    def _record_and_transition(
        self,
        url_id,
        classification: LivenessClassification,
        http_status: int | None,
        error: str | None,
        *,
        tls_status: str | None = None,
        redirect_target: str | None = None,
        checked_at: datetime,
    ) -> LivenessClassification:
        retry_count = self.liveness.retry_count(url_id)
        retryable = classification in {LivenessClassification.TEMPORARILY_UNAVAILABLE, LivenessClassification.UNKNOWN}
        retry_count = retry_count + 1 if retryable else 0
        retry_delay = min(self.retry_delay * (2 ** (retry_count - 1)), self.maximum_retry_delay)
        self.liveness.record_http(
            url_id,
            http_status,
            tls_status=tls_status,
            redirect_target=redirect_target,
            failure_reason=error,
            retry_after=(checked_at + retry_delay).isoformat() if retryable else None,
            retry_count=retry_count,
            recorded_at=checked_at.isoformat(),
        )
        current = self.candidates.get_state(url_id)
        target = _STATE_FOR_CLASSIFICATION[classification]
        if target != current and can_transition(current, target):
            self.candidates.transition(url_id, target, error)
        return classification
