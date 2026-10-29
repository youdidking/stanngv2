"""Bounded in-memory caches with TTL and periodic cleanup.

The bot never grows memory without bounds: every cache has a maximum size and
an expiry sweeper that is driven by the background scheduler.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable

from ..config import settings


class TTLCache:
    """Simple thread-safe-ish TTL cache with LRU eviction."""

    __slots__ = ("maxsize", "default_ttl", "_store", "_hits", "_misses", "name")

    def __init__(self, maxsize: int = 2048, default_ttl: float = 300.0, name: str = "cache"):
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self.name = name
        self._store: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: Hashable, default: Any = None) -> Any:
        now = time.monotonic()
        item = self._store.get(key)
        if item is None:
            self._misses += 1
            return default
        expires, value = item
        if expires <= now:
            self._store.pop(key, None)
            self._misses += 1
            return default
        self._store.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: Hashable, value: Any, ttl: float | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        self._store[key] = (time.monotonic() + ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def pop(self, key: Hashable, default: Any = None) -> Any:
        item = self._store.pop(key, None)
        if item is None:
            return default
        return item[1] if item[0] > time.monotonic() else default

    def delete(self, key: Hashable) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def cleanup(self) -> int:
        """Drop expired entries; returns the number of removed items."""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for key in expired:
            self._store.pop(key, None)
        return len(expired)

    def __len__(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "name": self.name,
            "size": len(self._store),
            "maxsize": self.maxsize,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }


class SlidingWindow:
    """Fixed-capacity deque of timestamps used for flood / raid detection."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[float] = []

    def add(self, now: float | None = None) -> None:
        self._events.append(now if now is not None else time.monotonic())

    def prune(self, window: float, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        cutoff = now - window
        kept = [ts for ts in self._events if ts >= cutoff]
        removed = len(self._events) - len(kept)
        self._events = kept
        return removed

    def count(self, window: float, now: float | None = None) -> int:
        self.prune(window, now)
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)


# --------------------------------------------------------------------------- #
# Global cache registry
# --------------------------------------------------------------------------- #
CACHES: list[TTLCache] = []

chat_settings_cache = TTLCache(maxsize=4096, default_ttl=180.0, name="chat_settings")
admin_rights_cache = TTLCache(maxsize=8192, default_ttl=120.0, name="admin_rights")
# panel message -> the user who opened it (an exclusive, per-admin panel)
panel_owner_cache = TTLCache(maxsize=8192, default_ttl=86400.0, name="panel_owner")
member_status_cache = TTLCache(maxsize=16384, default_ttl=120.0, name="member_status")
filter_cache = TTLCache(maxsize=2048, default_ttl=120.0, name="filters")
lock_cache = TTLCache(maxsize=2048, default_ttl=120.0, name="locks")
market_cache = TTLCache(maxsize=256, default_ttl=float(settings.market_cache_ttl), name="market")
role_cache = TTLCache(maxsize=16384, default_ttl=90.0, name="roles")
afk_cache = TTLCache(maxsize=4096, default_ttl=300.0, name="afk")

for _c in (chat_settings_cache, admin_rights_cache, panel_owner_cache, member_status_cache,
           filter_cache,
           lock_cache, market_cache, role_cache, afk_cache):
    CACHES.append(_c)


def invalidate_chat(chat_id: int) -> None:
    """Drop every cached value belonging to a chat (called after config edits)."""
    for cache in (chat_settings_cache, filter_cache, lock_cache):
        for key in [k for k in cache._store if isinstance(k, (tuple, list)) and k and k[0] == chat_id]:
            cache.delete(key)
        cache.delete(chat_id)
    for key in [k for k in role_cache._store if isinstance(k, (tuple, list)) and k and k[0] == chat_id]:
        role_cache.delete(key)
    for key in [k for k in admin_rights_cache._store if isinstance(k, (tuple, list)) and k and k[0] == chat_id]:
        admin_rights_cache.delete(key)
    for key in [k for k in member_status_cache._store if isinstance(k, (tuple, list)) and k and k[0] == chat_id]:
        member_status_cache.delete(key)


def invalidate_user(chat_id: int, user_id: int) -> None:
    role_cache.delete((chat_id, user_id))
    admin_rights_cache.delete((chat_id, user_id))
    member_status_cache.delete((chat_id, user_id))


def cleanup_all() -> dict[str, int]:
    return {cache.name: cache.cleanup() for cache in CACHES}


def clear_all() -> None:
    """Drop **every** cached entry (tests, maintenance, hot reload)."""
    for cache in CACHES:
        cache.clear()


def stats_all() -> list[dict[str, Any]]:
    return [cache.stats() for cache in CACHES]


def ttl_cache(cache: TTLCache, ttl: float | None = None,
              key_builder: Callable[..., Hashable] | None = None):
    """Async decorator memoizing a coroutine function in a :class:`TTLCache`."""

    def decorator(func: Callable[..., Awaitable[Any]]):
        async def wrapper(*args, **kwargs):
            key = key_builder(*args, **kwargs) if key_builder else (args, tuple(sorted(kwargs.items())))
            cached = cache.get(key, default=_MISSING)
            if cached is not _MISSING:
                return cached
            value = await func(*args, **kwargs)
            cache.set(key, value, ttl)
            return value

        wrapper.__name__ = getattr(func, "__name__", "wrapped")
        wrapper.cache = cache  # type: ignore[attr-defined]
        wrapper.invalidate = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator


class _Missing:
    pass


_MISSING = _Missing()


async def cache_sweeper(interval: int | None = None, stop_event: asyncio.Event | None = None) -> None:
    """Background loop keeping every registered cache small."""
    interval = interval or settings.cache_cleanup_interval
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break
        cleanup_all()
