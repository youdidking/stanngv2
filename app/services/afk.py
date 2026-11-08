"""AFK (away from keyboard) support."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.normalization import normalize_text
from ..core.timeutils import persian_relative
from ..db.models import User

logger = logging.getLogger("armando.afk")


async def set_afk(session: AsyncSession, user_id: int, reason: str = "") -> None:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name="")
        session.add(user)
    user.afk = True
    user.afk_reason = (reason or "").strip()[:300]
    user.afk_since = datetime.utcnow()
    await session.flush()
    cache.afk_cache.set(user_id, (True, user.afk_reason, user.afk_since))


async def clear_afk(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    if user is None or not user.afk:
        cache.afk_cache.delete(user_id)
        return False
    user.afk = False
    user.afk_reason = None
    user.afk_since = None
    await session.flush()
    cache.afk_cache.set(user_id, (False, "", None))
    return True


async def get_afk(session: AsyncSession, user_id: int) -> tuple[bool, str, datetime | None]:
    cached = cache.afk_cache.get(user_id)
    if cached is not None:
        return cached[0], cached[1] or "", cached[2]
    user = await session.get(User, user_id)
    if user is None:
        return False, "", None
    data = (bool(user.afk), user.afk_reason or "", user.afk_since)
    cache.afk_cache.set(user_id, data)
    return data


def afk_notice(name: str, reason: str, since: datetime | None) -> str:
    text = f"💤 {name} در حال حاضر در دسترس نیست (AFK)."
    if reason:
        text += f"\n📝 دلیل: {reason}"
    if since:
        text += f"\n🕒 از {persian_relative(since)}"
    return text


def mentioned_user_ids(message) -> list[int]:
    """User ids mentioned in a message (entities + reply)."""
    ids: list[int] = []
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and reply.from_user is not None:
        ids.append(reply.from_user.id)
    text = message.text or message.caption or ""
    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user:
            ids.append(entity.user.id)
        elif entity.type == "mention":
            # @username mentions carry no user id: the caller resolves them
            # through its known-user map.
            pass
    return [i for i in ids if i > 0]


def returns_from_afk(text: str) -> bool:
    """Heuristic: an ordinary message means the user is back."""
    normalized = normalize_text(text or "", mode="command")
    return len(normalized) > 1
