"""Helpers for owner broadcasts and short-lived media-group message IDs."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from ..core.normalization import normalize_text


class MediaGroupCache:
    """Remember Telegram message IDs briefly so albums can be copied intact."""

    def __init__(self, ttl_seconds: int = 900, max_groups: int = 500) -> None:
        self.ttl_seconds = max(60, ttl_seconds)
        self.max_groups = max(50, max_groups)
        self._groups: dict[tuple[int, str], tuple[float, set[int]]] = {}
        self._lock = threading.Lock()

    def remember(self, message: Message) -> None:
        if not message.media_group_id:
            return
        key = (message.chat.id, str(message.media_group_id))
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            updated_at, ids = self._groups.get(key, (now, set()))
            ids.add(message.message_id)
            self._groups[key] = (now, ids)
            while len(self._groups) > self.max_groups:
                oldest = min(self._groups, key=lambda item: self._groups[item][0])
                self._groups.pop(oldest, None)

    def get_message_ids(self, chat_id: int, media_group_id: str) -> list[int]:
        now = time.monotonic()
        key = (chat_id, str(media_group_id))
        with self._lock:
            self._prune(now)
            item = self._groups.get(key)
            return sorted(item[1]) if item else []

    def _prune(self, now: float) -> None:
        expired = [key for key, (updated_at, _ids) in self._groups.items()
                   if now - updated_at > self.ttl_seconds]
        for key in expired:
            self._groups.pop(key, None)


media_group_cache = MediaGroupCache()


class MediaGroupCacheMiddleware(BaseMiddleware):
    """Cache album IDs before router filters decide whether to handle a message."""

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any,
                       data: dict[str, Any]) -> Any:
        if isinstance(event, Message):
            media_group_cache.remember(event)
        return await handler(event, data)


def extract_broadcast_text(raw_text: str, command_phrase: str) -> str:
    """Remove an exact command phrase while preserving the original text body."""
    raw = (raw_text or "").strip()
    wanted = normalize_text(command_phrase, mode="command").split()
    if not raw or not wanted:
        return ""

    found: list[str] = []
    end = 0
    for match in re.finditer(r"\S+", raw):
        token_words = normalize_text(match.group(0), mode="command").split()
        if not token_words:
            continue
        found.extend(token_words)
        if wanted[:len(found)] != found:
            return ""
        end = match.end()
        if found == wanted:
            break
    if found != wanted:
        return ""

    body = raw[end:].strip()
    if len(body) >= 2:
        if body[0] == body[-1] and body[0] in {"\"", "'"}:
            body = body[1:-1]
        elif body[0] == "«" and body[-1] == "»":
            body = body[1:-1]
    return body.strip()
