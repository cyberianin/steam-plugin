from __future__ import annotations

import asyncio
import email.utils
import time
from urllib.parse import urlsplit

import httpx


class SteamHttpPolicy:
    """Host allowlist, per-host token bucket, and bounded transient retry policy."""

    ALLOWED_HOSTS = frozenset({"api.steampowered.com", "store.steampowered.com", "steamcommunity.com"})

    def __init__(self, capacity: float = 8, refill_per_second: float = 4):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: dict[str, tuple[float, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.ALLOWED_HOSTS or parsed.port not in (None, 443):
            raise ValueError("Steam HTTP request target is not allowlisted")
        return parsed.hostname

    async def acquire(self, host: str) -> None:
        if host not in self.ALLOWED_HOSTS:
            raise ValueError("Steam HTTP host is not allowlisted")
        lock = self._locks.setdefault(host, asyncio.Lock())
        while True:
            async with lock:
                now = time.monotonic()
                tokens, updated = self._buckets.get(host, (self.capacity, now))
                tokens = min(self.capacity, tokens + (now - updated) * self.refill_per_second)
                if tokens >= 1:
                    self._buckets[host] = (tokens - 1, now)
                    return
                wait = (1 - tokens) / self.refill_per_second
                self._buckets[host] = (tokens, now)
            await asyncio.sleep(wait)

    @staticmethod
    def retry_delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(5.0, max(0.0, float(header)))
            except ValueError:
                try:
                    target = email.utils.parsedate_to_datetime(header).timestamp()
                    return min(5.0, max(0.0, target - time.time()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(2.0, 0.2 * (2**attempt))


steam_http_policy = SteamHttpPolicy()
