"""Secure private-chat connection mode for administrators."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import BotRole, Chat, Connection
from .permissions import fetch_chat_member

logger = logging.getLogger("armando.connection")


async def get_active(session: AsyncSession, user_id: int) -> int | None:
    result = await session.execute(
        select(Connection).where(Connection.user_id == user_id, Connection.active.is_(True))
        .order_by(Connection.last_used_at.desc())
    )
    row = result.scalars().first()
    return row.chat_id if row else None


async def set_active(session: AsyncSession, user_id: int, chat_id: int) -> None:
    await session.execute(
        update(Connection).where(Connection.user_id == user_id)
        .values(active=False)
    )
    result = await session.execute(
        select(Connection).where(Connection.user_id == user_id, Connection.chat_id == chat_id)
    )
    row = result.scalar_one_or_none()
    now = datetime.utcnow()
    if row is None:
        session.add(Connection(user_id=user_id, chat_id=chat_id, active=True,
                               connected_at=now, last_used_at=now))
    else:
        row.active = True
        row.last_used_at = now
    await session.flush()


async def disconnect(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(
        update(Connection).where(Connection.user_id == user_id, Connection.active.is_(True))
        .values(active=False)
    )
    await session.flush()
    return bool(result.rowcount)


async def recent(session: AsyncSession, user_id: int, limit: int = 10) -> list[Connection]:
    result = await session.execute(
        select(Connection).where(Connection.user_id == user_id)
        .order_by(Connection.last_used_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def touch(session: AsyncSession, user_id: int, chat_id: int) -> None:
    result = await session.execute(
        select(Connection).where(Connection.user_id == user_id, Connection.chat_id == chat_id)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.last_used_at = datetime.utcnow()
        await session.flush()


async def is_authorized(bot: Bot, session: AsyncSession, user_id: int, chat_id: int) -> bool:
    """Connection may only be created for chats the user really administrates."""
    member = await fetch_chat_member(bot, chat_id, user_id)
    if member is not None and member.status in {"administrator", "creator"}:
        return True
    result = await session.execute(
        select(BotRole).where(BotRole.chat_id == chat_id, BotRole.user_id == user_id)
    )
    return result.scalar_one_or_none() is not None


async def available_chats(bot: Bot, session: AsyncSession, user_id: int,
                          limit: int = 12) -> list[tuple[int, str]]:
    """Chats known to the bot where ``user_id`` is an administrator."""
    chats: list[tuple[int, str]] = []
    result = await session.execute(
        select(BotRole.chat_id).where(BotRole.user_id == user_id).distinct()
    )
    role_chat_ids = [row[0] for row in result.all()]
    result = await session.execute(
        select(Chat).where(Chat.is_active.is_(True)).order_by(Chat.last_seen_at.desc()).limit(limit * 3)
    )
    candidates = list(result.scalars().all())
    for chat in candidates:
        if len(chats) >= limit:
            break
        if chat.id in role_chat_ids:
            chats.append((chat.id, chat.title))
            continue
        member = await fetch_chat_member(bot, chat.id, user_id)
        if member is not None and member.status in {"administrator", "creator"}:
            chats.append((chat.id, chat.title))
    return chats


async def drop_chat(session: AsyncSession, chat_id: int) -> int:
    result = await session.execute(delete(Connection).where(Connection.chat_id == chat_id))
    await session.flush()
    return int(result.rowcount or 0)
