"""Owner-level **global ban** (GBan).

One id, every chat the bot guards: as soon as a globally banned user shows up
in a group or a channel where the bot is an administrator, they are banned
again - no extra action from the local admins.

The feature is OFF unless the owner sets ``GLOBAL_BAN_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache as core_cache
from ..core.errors import safe_call, safe_delete, safe_send
from ..db.models import Chat, User

logger = logging.getLogger("armando.gban")


class _Missing:
    """Sentinel: distinguishes "not cached" from "cached as not banned"."""
    pass


_MISSING = _Missing()

# ``user_id -> reason | None`` - keeps the hot message path free of queries.
_gban_cache = core_cache.TTLCache(maxsize=8192, default_ttl=120.0, name="gban")
core_cache.CACHES.append(_gban_cache)


def invalidate(user_id: int) -> None:
    """Forget the cached state of a user (called after ban / unban)."""
    _gban_cache.delete(int(user_id))


async def is_banned(session: AsyncSession, user_id: int) -> str | None:
    """Reason when ``user_id`` is globally banned, otherwise ``None``."""
    key = int(user_id)
    cached = _gban_cache.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    result = await session.execute(
        select(User.global_banned, User.global_ban_reason).where(User.id == key))
    row = result.first()
    reason: str | None = None
    if row is not None and bool(row[0]):
        reason = row[1] or "بدون دلیل"
    _gban_cache.set(key, reason)
    return reason


async def ban_in_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Ban the user in one chat; ``False`` when the bot has no right to."""
    ok = await safe_call(
        lambda: bot.ban_chat_member(chat_id=chat_id, user_id=user_id,
                                    revoke_messages=False),
        default=None, context="gban_ban")
    return ok is not None and ok is not False


async def unban_in_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    ok = await safe_call(
        lambda: bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True),
        default=None, context="gban_unban")
    return ok is not None and ok is not False


async def enforce(bot: Bot, session: AsyncSession, *, chat_id: int, chat_title: str,
                  user_id: int, user_name: str, reason: str,
                  message_id: int | None = None, clean: bool = False) -> bool:
    """Ban a globally banned member and explain why in the chat."""
    banned = await ban_in_chat(bot, chat_id, user_id)
    if not banned:
        return False
    try:
        from .ban_reports import report_successful_ban

        await report_successful_ban(
            bot, session, chat_id=chat_id, target_id=user_id, actor_id=bot.id,
            reason=reason, source="global_ban", chat_title=chat_title,
            target_name=user_name, action="ban")
    except Exception as exc:  # noqa: BLE001 - report errors must not undo enforcement
        logger.warning("global-ban report failed (%s)", exc.__class__.__name__)
    if clean and message_id is not None:
        await safe_delete(bot, chat_id, message_id, context="gban_cleanup")
    await safe_send(
        bot, chat_id,
        "🌍 <b>بن سراسری</b>\n\n"
        f"<a href=\"tg://user?id={user_id}\">{user_name or user_id}</a> "
        "در لیست بن سراسری ربات است و از این گفتگو حذف شد.\n"
        f"📌 دلیل: {reason}",
        parse_mode="HTML")
    logger.info("gban enforced user=%s chat=%s", user_id, chat_id)
    return True


async def guarded_chat_ids(session: AsyncSession, *, skip: int | None = None) -> list[int]:
    """Every chat the bot is still part of (groups **and** channels).

    Chats where the bot is not an administrator simply fail the ban call and
    are skipped silently - there is no way to know the rights up front.
    """
    result = await session.execute(select(Chat.id).where(Chat.is_active.is_(True)))
    ids = [int(row[0]) for row in result.all()]
    if skip is not None:
        ids = [cid for cid in ids if cid != int(skip)]
    return ids


async def enforce_everywhere(bot: Bot, session: AsyncSession, user_id: int, reason: str,
                             *, chat_ids: Iterable[int] | None = None, skip: int | None = None,
                             delay: float = 0.15) -> int:
    """Ban the user in every guarded chat; returns the number of successes."""
    ids = list(chat_ids) if chat_ids is not None else await guarded_chat_ids(session, skip=skip)
    done = 0
    for chat_id in ids:
        if await ban_in_chat(bot, chat_id, user_id):
            done += 1
            try:
                from .ban_reports import report_successful_ban

                await report_successful_ban(
                    bot, session, chat_id=chat_id, target_id=user_id,
                    actor_id=bot.id, reason=reason, source="global_ban",
                    action="ban")
            except Exception as exc:  # noqa: BLE001 - keep the federation/global sweep running
                logger.warning("global-ban report failed chat=%s (%s)",
                               chat_id, exc.__class__.__name__)
        await asyncio.sleep(delay)   # stay well below the Telegram flood limits
    return done


async def lift_everywhere(bot: Bot, session: AsyncSession, user_id: int, *,
                          chat_ids: Iterable[int] | None = None, skip: int | None = None,
                          delay: float = 0.15) -> int:
    """Undo the global ban in every guarded chat."""
    ids = list(chat_ids) if chat_ids is not None else await guarded_chat_ids(session, skip=skip)
    done = 0
    for chat_id in ids:
        if await unban_in_chat(bot, chat_id, user_id):
            done += 1
        await asyncio.sleep(delay)
    return done
