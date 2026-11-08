"""Rate limiting primitives (users, callbacks, reports, captcha, anti-spam)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Hashable

from ..config import settings


@dataclass
class Bucket:
    events: list[float] = field(default_factory=list)

    def allow(self, limit: int, window: float, now: float | None = None) -> tuple[bool, float]:
        now = now or time.monotonic()
        cutoff = now - window
        self.events = [ts for ts in self.events if ts >= cutoff]
        if len(self.events) >= limit:
            retry_after = max(0.05, self.events[0] + window - now)
            return False, retry_after
        self.events.append(now)
        return True, 0.0

    def count(self, window: float, now: float | None = None) -> int:
        now = now or time.monotonic()
        cutoff = now - window
        self.events = [ts for ts in self.events if ts >= cutoff]
        return len(self.events)

    def empty(self) -> bool:
        return not self.events


class RateLimiter:
    """Keyed sliding-window limiter with automatic pruning."""

    def __init__(self, default_limit: int = 10, default_window: float = 30.0, max_keys: int = 100_000):
        self.default_limit = default_limit
        self.default_window = default_window
        self.max_keys = max_keys
        self._buckets: dict[Hashable, Bucket] = {}

    def check(self, key: Hashable, limit: int | None = None,
              window: float | None = None) -> tuple[bool, float]:
        limit = limit if limit is not None else self.default_limit
        window = window if window is not None else self.default_window
        bucket = self._buckets.setdefault(key, Bucket())
        allowed, retry = bucket.allow(limit, window)
        if len(self._buckets) > self.max_keys:
            self.prune(window)
        return allowed, retry

    def is_limited(self, key: Hashable, limit: int | None = None, window: float | None = None) -> bool:
        return not self.check(key, limit, window)[0]

    def retry_after(self, key: Hashable, limit: int | None = None, window: float | None = None) -> float:
        return self.check(key, limit, window)[1]

    def reset(self, key: Hashable) -> None:
        self._buckets.pop(key, None)

    def clear(self) -> None:
        """Drop every bucket (tests, hot reload)."""
        self._buckets.clear()

    def prune(self, window: float | None = None, max_idle: float = 3600.0) -> int:
        now = time.monotonic()
        window = window or self.default_window
        removed = 0
        for key in list(self._buckets):
            bucket = self._buckets[key]
            bucket.count(max(window, max_idle))
            if bucket.empty():
                self._buckets.pop(key, None)
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._buckets)


command_limiter = RateLimiter(settings.command_rate_limit, settings.command_rate_window, max_keys=50_000)
callback_limiter = RateLimiter(settings.callback_rate_limit, settings.callback_rate_window, max_keys=50_000)
report_limiter = RateLimiter(settings.report_rate_limit, settings.report_rate_window, max_keys=20_000)
captcha_limiter = RateLimiter(6, 30.0, max_keys=20_000)
message_limiter = RateLimiter(20, 10.0, max_keys=100_000)

ALL_LIMITERS = (command_limiter, callback_limiter, report_limiter, captcha_limiter, message_limiter)


def reset_all() -> None:
    """Clear every limiter (used by the test-suite, never in production)."""
    for limiter in ALL_LIMITERS:
        limiter.clear()


def prune_all() -> int:
    return sum(limiter.prune() for limiter in ALL_LIMITERS)
