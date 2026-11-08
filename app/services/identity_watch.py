"""Announce name / username changes of group members.

Telegram sends **no** update when somebody edits their profile, so there is no
``chat_member`` event to listen to.  The only reliable signal is the
``from_user`` payload attached to every update the bot receives: comparing it
with the row stored in ``users`` reveals the change.  This is the same
technique well-known watcher bots (for example @SangMata_bot) rely on.

The service is deliberately cheap:

* one primary-key lookup per message,
* a short in-memory debounce so a busy group does not hammer the database,
* it never raises - a failed notice must never break the update pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("armando.identity_watch")

# How long a (chat, user) pair is skipped after being checked.  The stored row
# is updated the moment a change is seen, so a later update cannot re-announce
# the same change - the debounce only saves work, it cannot lose an event.
_DEBOUNCE_SECONDS = 20.0
_recent: dict[tuple[int, int], float] = {}


def _display(first_name: str | None, last_name: str | None, username: str | None) -> str:
    name = f"{first_name or ''} {last_name or ''}".strip()
    if name:
        return name
    return f"@{username}" if username else "—"


def _user_line(first_name: str | None, last_name: str | None, username: str | None) -> str:
    """«حسن با آیدی @hassan73273» - the wording the group owners asked for."""
    name = f"{first_name or ''} {last_name or ''}".strip() or "بدون نام"
    if username:
        return f"{name} با آیدی @{username}"
    return f"{name} (بدون آیدی)"


async def watch_identity(bot: Bot, session: AsyncSession, chat_id: int,
                         user: Any) -> str | None:
    """Compare ``user`` with the stored row and announce any difference.

    Returns the sent notice, or ``None`` when there was nothing to report.
    """
    if user is None or getattr(user, "is_bot", False):
        return None
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id:
        return None

    now = time.monotonic()
    key = (chat_id, user_id)
    last = _recent.get(key, 0.0)
    if now - last < _DEBOUNCE_SECONDS:
        return None
    _recent[key] = now
    if len(_recent) > 5000:  # keep the guard bounded on busy public bots
        _recent.clear()

    try:
        from ..db.models import User

        settings = await _settings(session, chat_id)
        announce_name = bool(settings.get("announce_name_change", True))
        announce_username = bool(settings.get("announce_username_change", True))
        if not announce_name and not announce_username:
            return None

        row = await session.get(User, user_id)
        if row is None:
            return None  # first sighting: nothing to compare against yet

        old_name = f"{row.first_name or ''} {row.last_name or ''}".strip()
        new_name = f"{getattr(user, 'first_name', '') or ''} " \
                   f"{getattr(user, 'last_name', '') or ''}".strip()
        old_username = (row.username or "").lstrip("@").lower()
        new_username = (getattr(user, "username", "") or "").lstrip("@").lower()

        name_changed = bool(new_name) and old_name != new_name
        username_changed = old_username != new_username
        if name_changed and not announce_name:
            name_changed = False
        if username_changed and not announce_username:
            username_changed = False
        if not name_changed and not username_changed:
            return None

        # Persist first: even if sending fails the change is not announced twice.
        row.first_name = getattr(user, "first_name", "") or ""
        row.last_name = getattr(user, "last_name", None)
        row.username = getattr(user, "username", None)
        session.add(row)

        before = _user_line(
            old_name.split(" ")[0] if old_name else "",
            " ".join(old_name.split(" ")[1:]) if len(old_name.split(" ")) > 1 else None,
            old_username or None)
        after = _user_line(getattr(user, "first_name", ""), getattr(user, "last_name", None),
                           new_username or None)

        lines = ["🔄 <b>تغییر مشخصات کاربر</b>", ""]
        if name_changed:
            lines.append(f"👤 از {before}")
            lines.append(f"👤 به {after}")
        else:
            lines.append(f"👤 {after}")
        if username_changed:
            old_id = f"@{old_username}" if old_username else "بدون آیدی"
            new_id = f"@{new_username}" if new_username else "بدون آیدی"
            lines.append(f"🔗 آیدی: <code>{old_id}</code> → <code>{new_id}</code>")
        lines.append(f"🆔 شناسه: <code>{user_id}</code>")

        text = "\n".join(lines)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        logger.info("identity change announced user=%s chat=%s", user_id, chat_id)
        return text
    except Exception as exc:  # noqa: BLE001 - never break the update pipeline
        logger.debug("identity watch failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return None


async def _settings(session: AsyncSession, chat_id: int) -> dict:
    try:
        from .chat_state import get_settings_cached

        return await get_settings_cached(session, chat_id)
    except Exception:  # noqa: BLE001
        return {}
