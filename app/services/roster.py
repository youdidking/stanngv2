"""Who is a member of this chat, as far as the bot can know.

Telegram never gives a bot the full member list, so the bot builds its own
roster out of **every** signal Telegram delivers:

* messages (and commands) written in the chat
* join / leave service messages
* ``chat_member`` updates (somebody is promoted, restricted, unmuted, ...)
* reactions on messages (the quiet members!)
* the administrator list, which *is* available (``getChatAdministrators``)
* historical activity rows (``user_activity``)

Every one of those sources is optional: the roster is the union of whatever
the bot has managed to observe so far.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..db.models import ChatMemberState, User, UserActivity
from .chat_state import ensure_chat

logger = logging.getLogger("armando.roster")

# Statuses that mean "no longer in the chat".
EXCLUDED_STATUSES = ("left", "kicked")
# Telegram's own pseudo accounts (anonymous admins, service bots).
ANONYMOUS_IDS = frozenset({777000, 1087968824, 136817688, 1087968824})


async def remember(session: AsyncSession, chat_id: int, user: TelegramUser | None, *,
                   status: str | None = None, left: bool | None = None) -> None:
    """Make sure ``user`` is on the roster of ``chat_id`` (cheap upsert)."""
    if user is None or getattr(user, "is_bot", False):
        return
    user_id = int(user.id)
    if user_id in ANONYMOUS_IDS:
        return

    await ensure_chat(session, chat_id)
    db_user = await session.get(User, user_id)
    if db_user is None:
        db_user = User(id=user_id)
        session.add(db_user)
        await session.flush()
    # keep the displayed name fresh
    if getattr(user, "first_name", None):
        db_user.first_name = (user.first_name or "")[:128]
    if getattr(user, "last_name", None) is not None:
        db_user.last_name = (user.last_name or None)
    if getattr(user, "username", None):
        db_user.username = user.username

    result = await session.execute(
        select(ChatMemberState).where(ChatMemberState.chat_id == chat_id,
                                      ChatMemberState.user_id == user_id))
    state = result.scalar_one_or_none()
    now = datetime.utcnow()
    if state is None:
        state = ChatMemberState(chat_id=chat_id, user_id=user_id, status="member",
                                joined_at=now)
        session.add(state)
    if status:
        state.status = status
    elif state.status in EXCLUDED_STATUSES:
        # Seen again in the chat (a message, a reaction, ...): they are back.
        state.status = "member"
        state.left_at = None
    if left is True:
        state.status = "left"
        state.left_at = now
    elif left is False:
        state.left_at = None
    state.updated_at = now


async def _roster_ids(session: AsyncSession, chat_id: int) -> set[int]:
    """Member ids the bot has observed in this chat (both tables)."""
    ids: set[int] = set()

    member_rows = await session.execute(
        select(ChatMemberState.user_id, ChatMemberState.status, ChatMemberState.left_at)
        .where(ChatMemberState.chat_id == chat_id))
    gone: set[int] = set()
    for user_id, status, left_at in member_rows.all():
        if (status or "member") in EXCLUDED_STATUSES or left_at is not None:
            gone.add(int(user_id))
        else:
            ids.add(int(user_id))

    activity_rows = await session.execute(
        select(UserActivity.user_id).where(UserActivity.chat_id == chat_id).distinct())
    ids.update(int(row[0]) for row in activity_rows.all())
    return ids - gone


async def _admins(bot: Bot, chat_id: int) -> dict[int, TelegramUser]:
    """Administrators of the chat - the one list Telegram really provides."""
    members = await safe_call(lambda: bot.get_chat_administrators(chat_id=chat_id),
                              default=None, context="roster_admins")
    found: dict[int, TelegramUser] = {}
    if not members:
        return found
    try:
        for member in members:
            user = getattr(member, "user", None)
            if user is None or getattr(user, "is_bot", False):
                continue
            user_id = int(user.id)
            if user_id in ANONYMOUS_IDS:
                continue
            found[user_id] = user
    except Exception:  # noqa: BLE001 - a malformed payload must not break it
        logger.debug("could not read the administrator list chat=%s", chat_id)
    return found


def _name_of(first_name, last_name, username) -> str:
    parts = [p for p in ((first_name or "").strip(), (last_name or "").strip()) if p]
    if parts:
        return " ".join(parts)
    if username:
        return f"@{username}"
    return "عضو"


async def known_members(session: AsyncSession, chat_id: int, *, bot: Bot | None = None,
                        limit: int = 1000) -> list[tuple[int, str]]:
    """Everybody the bot believes is in this chat: ``(user_id, name)`` pairs."""
    ids = await _roster_ids(session, chat_id)
    admins = await _admins(bot, chat_id) if bot is not None else {}
    ids |= set(admins)
    ids = {uid for uid in ids if uid > 0 and uid not in ANONYMOUS_IDS}
    if not ids:
        return []

    result = await session.execute(
        select(User.id, User.first_name, User.last_name, User.username, User.is_bot)
        .where(User.id.in_(sorted(ids)[:limit])))
    rows = {int(row[0]): row for row in result.all()}

    members: list[tuple[int, str]] = []
    for user_id in sorted(ids)[:limit]:
        row = rows.get(user_id)
        if row is not None:
            if bool(row[4]):
                continue
            members.append((user_id, _name_of(row[1], row[2], row[3])))
        elif user_id in admins:
            # An administrator Telegram told us about: add them to the roster.
            user = admins[user_id]
            members.append((user_id, _name_of(user.first_name, user.last_name,
                                              user.username)))
            await remember(session, chat_id, user)
    return members
