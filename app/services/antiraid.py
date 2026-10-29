"""Anti-raid detection: rapid join bursts."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..core.normalization import normalize_text

logger = logging.getLogger("armando.antiraid")

SUSPICIOUS_NAME_MARKERS = ("http", "t.me", "telegram", "www.", ".com", ".ir", "joinchat")


@dataclass
class RaidState:
    joins: list[tuple[float, int]] = field(default_factory=list)
    active_until: float = 0.0
    alerted: bool = False


class RaidTracker:
    def __init__(self) -> None:
        self._states: dict[int, RaidState] = defaultdict(RaidState)

    def record_join(self, chat_id: int, user_id: int, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        state = self._states[chat_id]
        state.joins.append((now, user_id))
        return len(self._joins_within(state, 3600.0, now))

    def _joins_within(self, state: RaidState, window: float, now: float | None = None) -> list[int]:
        now = now if now is not None else time.monotonic()
        cutoff = now - window
        state.joins = [(ts, uid) for ts, uid in state.joins if ts >= cutoff]
        return [uid for _, uid in state.joins]

    def is_raid(self, chat_id: int, threshold: int, window: int) -> bool:
        state = self._states.get(chat_id)
        if not state:
            return False
        return len(self._joins_within(state, float(window))) >= max(2, threshold)

    def raid_users(self, chat_id: int, window: int) -> list[int]:
        state = self._states.get(chat_id)
        if not state:
            return []
        return self._joins_within(state, float(window))

    def activate(self, chat_id: int, seconds: int = 900) -> None:
        self._states[chat_id].active_until = time.monotonic() + seconds

    def is_active(self, chat_id: int) -> bool:
        state = self._states.get(chat_id)
        return bool(state and state.active_until > time.monotonic())

    def deactivate(self, chat_id: int) -> None:
        state = self._states.get(chat_id)
        if state:
            state.active_until = 0.0
            state.alerted = False
            state.joins.clear()

    def remaining(self, chat_id: int) -> int:
        state = self._states.get(chat_id)
        if not state:
            return 0
        return max(0, int(state.active_until - time.monotonic()))

    def mark_alerted(self, chat_id: int) -> None:
        self._states[chat_id].alerted = True

    def alerted(self, chat_id: int) -> bool:
        state = self._states.get(chat_id)
        return bool(state and state.alerted)

    def prune(self, max_age: float = 7200.0) -> int:
        now = time.monotonic()
        removed = 0
        for chat_id in list(self._states):
            state = self._states[chat_id]
            self._joins_within(state, max_age)
            if not state.joins and state.active_until <= now:
                self._states.pop(chat_id, None)
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._states)


raid_tracker = RaidTracker()


def looks_suspicious(user) -> bool:
    """Heuristic: profile names that advertise links or are empty."""
    if user is None:
        return False
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    username = (getattr(user, "username", "") or "").strip()
    full = normalize_text(f"{first} {last} {username}", mode="aggressive")
    if not full:
        return True
    return any(marker in full for marker in SUSPICIOUS_NAME_MARKERS)
