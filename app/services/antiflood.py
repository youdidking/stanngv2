"""Sliding-window anti-spam tracking (flood, repeats, mentions, media bursts).

All state lives in memory with bounded size and TTL pruning, so the bot stays
fast under high message volume without unbounded growth.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..core.normalization import count_mentions, normalize_text, similarity

logger = logging.getLogger("armando.antiflood")

MAX_TRACKED_KEYS = 50_000


@dataclass
class UserWindow:
    timestamps: list[float] = field(default_factory=list)
    texts: list[tuple[float, str]] = field(default_factory=list)
    media: list[float] = field(default_factory=list)
    mentions: list[int] = field(default_factory=list)


class FloodTracker:
    """Per (chat, user) sliding windows."""

    def __init__(self, max_keys: int = MAX_TRACKED_KEYS) -> None:
        self._windows: dict[tuple[int, int], UserWindow] = defaultdict(UserWindow)
        self.max_keys = max_keys

    # ------------------------------------------------------------------ record
    def record(self, chat_id: int, user_id: int, *, text: str = "", media: bool = False,
               mentions: int = 0, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        window = self._windows[(chat_id, user_id)]
        window.timestamps.append(now)
        if text:
            window.texts.append((now, normalize_text(text, mode="aggressive")))
            if len(window.texts) > 12:
                window.texts = window.texts[-12:]
        if media:
            window.media.append(now)
        if mentions:
            window.mentions.append(mentions)
        if len(window.timestamps) > 60:
            window.timestamps = window.timestamps[-60:]
        if len(self._windows) > self.max_keys:
            self.prune(max_age=120.0)

    # ------------------------------------------------------------------ checks
    def flood_count(self, chat_id: int, user_id: int, window_seconds: float,
                    now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        window = self._windows.get((chat_id, user_id))
        if window is None:
            return 0
        cutoff = now - window_seconds
        window.timestamps = [ts for ts in window.timestamps if ts >= cutoff]
        return len(window.timestamps)

    def is_flood(self, chat_id: int, user_id: int, limit: int, window_seconds: float) -> bool:
        return self.flood_count(chat_id, user_id, window_seconds) > max(1, limit)

    def repeated_count(self, chat_id: int, user_id: int, text: str,
                       window_seconds: float = 30.0, threshold: float = 0.85) -> int:
        window = self._windows.get((chat_id, user_id))
        if window is None or not text:
            return 0
        now = time.monotonic()
        cutoff = now - window_seconds
        normalized = normalize_text(text, mode="aggressive")
        count = 0
        for ts, previous in window.texts:
            if ts < cutoff:
                continue
            if previous == normalized or similarity(previous, normalized) >= threshold:
                count += 1
        return count

    def is_repeated(self, chat_id: int, user_id: int, text: str, limit: int,
                    window_seconds: float = 30.0) -> bool:
        return self.repeated_count(chat_id, user_id, text, window_seconds) >= max(2, limit)

    def mention_sum(self, chat_id: int, user_id: int, window_seconds: float = 20.0) -> int:
        window = self._windows.get((chat_id, user_id))
        if window is None:
            return 0
        now = time.monotonic()
        cutoff = now - window_seconds
        return sum(count for ts, count in zip(window.timestamps[-len(window.mentions):],
                                              window.mentions) if ts >= cutoff) if window.mentions else 0

    def is_mention_spam(self, chat_id: int, user_id: int, text: str, limit: int) -> bool:
        if limit <= 0:
            return False
        return count_mentions(text or "") >= max(2, limit)

    def media_count(self, chat_id: int, user_id: int, window_seconds: float) -> int:
        window = self._windows.get((chat_id, user_id))
        if window is None:
            return 0
        now = time.monotonic()
        cutoff = now - window_seconds
        window.media = [ts for ts in window.media if ts >= cutoff]
        return len(window.media)

    def is_media_flood(self, chat_id: int, user_id: int, limit: int, window_seconds: float) -> bool:
        if limit <= 0:
            return False
        return self.media_count(chat_id, user_id, window_seconds) > limit

    # ----------------------------------------------------------------- cleanup
    def reset_user(self, chat_id: int, user_id: int) -> None:
        self._windows.pop((chat_id, user_id), None)

    def prune(self, max_age: float = 300.0) -> int:
        now = time.monotonic()
        cutoff = now - max_age
        removed = 0
        for key in list(self._windows):
            window = self._windows[key]
            window.timestamps = [ts for ts in window.timestamps if ts >= cutoff]
            window.texts = [(ts, t) for ts, t in window.texts if ts >= cutoff]
            window.media = [ts for ts in window.media if ts >= cutoff]
            if not window.timestamps and not window.texts and not window.media:
                self._windows.pop(key, None)
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._windows)


tracker = FloodTracker()


# --------------------------------------------------------------------------- #
# Pipeline helper
# --------------------------------------------------------------------------- #
@dataclass
class SpamVerdict:
    spam: bool = False
    kind: str = ""       # flood | repeat | mention | media
    label_fa: str = ""


KIND_LABELS_FA = {
    "flood": "ارسال پیام پشت‌سرهم (فلاود)",
    "repeat": "تکرار پیام یکسان",
    "mention": "منشن بیش از حد",
    "media": "ارسال رسانه پشت‌سرهم",
}


def evaluate(chat_id: int, user_id: int, *, text: str = "", media: bool = False,
             antiflood_enabled: bool = True, flood_limit: int = 5, flood_window: int = 3,
             repeat_limit: int = 0, mention_limit: int = 0, media_limit: int = 0,
             media_window: int = 10) -> SpamVerdict:
    """Decide whether a fresh message is spam. ``text`` is pre-normalized text."""
    if antiflood_enabled and flood_limit > 0:
        if tracker.is_flood(chat_id, user_id, flood_limit, float(flood_window)):
            return SpamVerdict(True, "flood", KIND_LABELS_FA["flood"])
    if repeat_limit > 0 and text:
        if tracker.is_repeated(chat_id, user_id, text, repeat_limit):
            return SpamVerdict(True, "repeat", KIND_LABELS_FA["repeat"])
    if mention_limit > 0:
        if tracker.is_mention_spam(chat_id, user_id, text, mention_limit):
            return SpamVerdict(True, "mention", KIND_LABELS_FA["mention"])
    if media_limit > 0 and media:
        if tracker.is_media_flood(chat_id, user_id, media_limit, float(media_window)):
            return SpamVerdict(True, "media", KIND_LABELS_FA["media"])
    return SpamVerdict()
