"""Fetch, cache, and conservatively apply per-host robots.txt policies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from .fetch import SafeHttpProbe
from .robots import RobotsPolicy
from .state import HostRepository


class RobotsDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class RobotsManager:
    def __init__(self, hosts: HostRepository, probe: SafeHttpProbe, *, ttl: timedelta = timedelta(hours=24)) -> None:
        if ttl <= timedelta():
            raise ValueError("robots TTL must be positive")
        self.hosts = hosts
        self.probe = probe
        self.ttl = ttl

    def allows(self, url: str, user_agent: str, now: datetime | None = None) -> bool:
        return self.check(url, user_agent, now) is RobotsDecision.ALLOWED

    def crawl_delay(self, url: str, user_agent: str, now: datetime | None = None) -> float | None:
        current = now or datetime.now(UTC)
        policy = self.policy_for(url, current)
        return None if policy is None else policy.crawl_delay(user_agent, current)

    def check(self, url: str, user_agent: str, now: datetime | None = None) -> RobotsDecision:
        """Distinguish an explicit robots denial from an unavailable policy."""
        current = now or datetime.now(UTC)
        policy = self.policy_for(url, current)
        if policy is None:
            return RobotsDecision.UNAVAILABLE
        return RobotsDecision.ALLOWED if policy.permits(user_agent, url, current) else RobotsDecision.DENIED

    def policy_for(self, url: str, now: datetime) -> RobotsPolicy | None:
        parts = urlsplit(url)
        # robots policies are scoped to an origin; non-default ports must not
        # share a cached policy with the default-port site.
        host = parts.netloc.casefold()
        cached = self.hosts.robots(host)
        if cached is not None:
            body, expires_at = cached
            expiry = datetime.fromisoformat(expires_at)
            if now < expiry:
                return RobotsPolicy(body, now, expiry)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        fetched = self.probe.probe(robots_url)
        if fetched.status == 404:
            body = "User-agent: *\nAllow: /\n"
        elif fetched.status == 200:
            body = fetched.body.decode("utf-8", errors="replace")
        else:
            return None
        expires_at = now + self.ttl
        self.hosts.save_robots(host, body, expires_at.isoformat())
        return RobotsPolicy(body, now, expires_at)
