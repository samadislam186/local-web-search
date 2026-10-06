"""Conservative robots.txt policy parsing and cache-expiry checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.robotparser import RobotFileParser


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    body: str
    fetched_at: datetime
    expires_at: datetime

    def permits(self, user_agent: str, url: str, now: datetime) -> bool:
        """An expired policy is not trusted: caller must refresh before crawling."""
        if now >= self.expires_at:
            return False
        parser = RobotFileParser()
        parser.parse(self.body.splitlines())
        return parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str, now: datetime) -> float | None:
        """Return a positive Crawl-delay in seconds for the best matching user-agent group."""
        if now >= self.expires_at:
            return None
        requested = user_agent.casefold()
        groups: list[tuple[list[str], float | None]] = []
        agents: list[str] = []
        delay: float | None = None
        has_directive = False
        for raw_line in self.body.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            directive = name.casefold()
            if directive == "user-agent":
                if has_directive:
                    groups.append((agents, delay))
                    agents, delay, has_directive = [], None, False
                agents.append(value.casefold())
            elif agents:
                has_directive = True
                if directive == "crawl-delay":
                    try:
                        parsed = float(value)
                    except ValueError:
                        continue
                    if parsed >= 0:
                        delay = parsed
        if agents:
            groups.append((agents, delay))
        for candidates in ([requested], ["*"]):
            for group_agents, group_delay in groups:
                if any(agent in candidates for agent in group_agents) and group_delay is not None:
                    return group_delay
        return None
