"""Chat / user / member-state helpers plus cached settings access."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.normalization import normalize_text
from ..db.models import (
    Chat,
    ChatMemberState,
    ChatSettings,
    User,
)

logger = logging.getLogger("armando.state")

SETTINGS_COLUMNS: tuple[str, ...] = tuple(
    c.name for c in ChatSettings.__table__.columns  # type: ignore[attr-defined]
)


# --------------------------------------------------------------------------- #
# Upserts
# --------------------------------------------------------------------------- #
async def ensure_chat(session: AsyncSession, chat_id: int, *, title: str = "",
                      chat_type: str = "group") -> Chat:
    """Make sure a parent ``chats`` row exists (FK safe creation of child rows)."""
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        return chat
    now = datetime.utcnow()
    chat = Chat(id=chat_id, title=title or "", type=chat_type,
                first_seen_at=now, last_seen_at=now)
    session.add(chat)
    await session.flush()
    return chat


async def ensure_user(session: AsyncSession, user_id: int, *, user=None) -> User:
    """Make sure a parent ``users`` row exists (FK safe creation of child rows)."""
    obj = await session.get(User, user_id)
    if obj is not None:
        return obj
    obj = User(id=user_id, first_name=getattr(user, "first_name", "") or "",
               last_name=getattr(user, "last_name", None),
               username=getattr(user, "username", None),
               is_bot=bool(getattr(user, "is_bot", False)))
    session.add(obj)
    await session.flush()
    return obj


async def get_or_create_chat(session: AsyncSession, chat) -> Chat:
    obj = await session.get(Chat, chat.id)
    now = datetime.utcnow()
    if obj is None:
        obj = Chat(
            id=chat.id,
            title=getattr(chat, "title", "") or "",
            username=getattr(chat, "username", None),
            type=getattr(chat, "type", "group"),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(obj)
        await session.flush()
        return obj
    changed = False
    title = getattr(chat, "title", "") or ""
    username = getattr(chat, "username", None)
    chat_type = getattr(chat, "type", "group")
    if title and obj.title != title:
        obj.title, changed = title, True
    if username != obj.username:
        obj.username, changed = username, True
    if chat_type and obj.type != chat_type:
        obj.type, changed = chat_type, True
    # Any update/command from a chat proves the bot still has access to it.
    if not obj.is_active:
        obj.is_active, changed = True, True
    if changed:
        obj.last_seen_at = now
    return obj


async def get_or_create_user(session: AsyncSession, user) -> User:
    if user is None:
        raise ValueError("user is required")
    obj = await session.get(User, user.id)
    if obj is None:
        obj = User(
            id=user.id,
            first_name=getattr(user, "first_name", "") or "",
            last_name=getattr(user, "last_name", None),
            username=getattr(user, "username", None),
            is_bot=bool(getattr(user, "is_bot", False)),
            is_premium=bool(getattr(user, "is_premium", False)),
            language_code=getattr(user, "language_code", None),
        )
        session.add(obj)
        await session.flush()
        return obj
    changed = False
    for field_name in ("first_name", "last_name", "username", "is_bot", "is_premium", "language_code"):
        value = getattr(user, field_name, None)
        if value is not None and getattr(obj, field_name) != value:
            setattr(obj, field_name, value)
            changed = True
    if changed:
        obj.updated_at = datetime.utcnow()
    return obj


async def get_member_state(session: AsyncSession, chat_id: int, user_id: int,
                           *, user=None, create: bool = True) -> ChatMemberState | None:
    result = await session.execute(
        select(ChatMemberState).where(ChatMemberState.chat_id == chat_id,
                                      ChatMemberState.user_id == user_id)
    )
    state = result.scalar_one_or_none()
    if state is not None or not create:
        return state
    await ensure_chat(session, chat_id)
    await ensure_user(session, user_id, user=user)
    state = ChatMemberState(chat_id=chat_id, user_id=user_id, joined_at=datetime.utcnow())
    session.add(state)
    await session.flush()
    return state


async def get_member_state_snapshot(session: AsyncSession, chat_id: int, user_id: int) -> dict[str, Any]:
    key = (chat_id, user_id)
    cached = cache.role_cache.get(key)
    if cached is not None:
        return cached
    state = await get_member_state(session, chat_id, user_id, create=False)
    data: dict[str, Any] = {"is_trusted": False, "trust_bypass": [], "bot_role": None,
                            "warn_count": 0, "tag": None, "status": "member"}
    if state is not None:
        data.update({
            "is_trusted": bool(state.is_trusted),
            "trust_bypass": list(state.trust_bypass or []),
            "bot_role": state.bot_role,
            "warn_count": state.warn_count,
            "tag": state.tag,
            "status": state.status,
        })
    cache.role_cache.set(key, data)
    return data


def invalidate_member(chat_id: int, user_id: int) -> None:
    cache.invalidate_user(chat_id, user_id)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
async def get_settings(session: AsyncSession, chat_id: int) -> ChatSettings:
    settings_obj = await session.get(ChatSettings, chat_id)
    if settings_obj is None:
        await ensure_chat(session, chat_id)
        settings_obj = ChatSettings(chat_id=chat_id)
        session.add(settings_obj)
        await session.flush()
    return settings_obj


def _settings_to_dict(obj: ChatSettings) -> dict[str, Any]:
    data = {}
    for column in SETTINGS_COLUMNS:
        data[column] = getattr(obj, column)
    bypass = data.get("trust_bypass")
    if isinstance(bypass, str):
        try:
            import json

            data["trust_bypass"] = json.loads(bypass)
        except Exception:  # noqa: BLE001
            data["trust_bypass"] = []
    data.setdefault("trust_bypass", [])
    data.setdefault("disabled_commands", [])
    return data


async def get_settings_cached(session: AsyncSession, chat_id: int) -> dict[str, Any]:
    """Read-only snapshot used by the hot moderation pipeline."""
    cached = cache.chat_settings_cache.get(chat_id)
    if cached is not None:
        return cached
    obj = await get_settings(session, chat_id)
    data = _settings_to_dict(obj)
    cache.chat_settings_cache.set(chat_id, data)
    return data


async def display_names(session: AsyncSession, user_ids) -> dict[int, str]:
    """Resolve many user ids to display names with **one** query.

    Leaderboards used to call ``session.get(User, id)`` once per row, which is
    a classic N+1 - fine for three rows, wasteful for thirty.
    """
    ids = [int(uid) for uid in (user_ids or []) if uid]
    if not ids:
        return {}
    from ..db.models import User

    rows = (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    names: dict[int, str] = {}
    for user in rows:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        names[int(user.id)] = name or (f"@{user.username}" if user.username else str(user.id))
    return names


async def update_settings(session: AsyncSession, chat_id: int, **values) -> ChatSettings:
    obj = await get_settings(session, chat_id)
    for key, value in values.items():
        if key in SETTINGS_COLUMNS:
            setattr(obj, key, value)
    obj.updated_at = datetime.utcnow()
    session.add(obj)
    await session.flush()
    cache.chat_settings_cache.delete(chat_id)
    return obj


def invalidate_settings(chat_id: int) -> None:
    cache.chat_settings_cache.delete(chat_id)


DEFAULT_TRUST_BYPASS = ["locks", "filters", "antiflood", "antispam", "lang"]


async def set_trust(session: AsyncSession, chat_id: int, user_id: int, trusted: bool,
                    bypass: Iterable[str] | None = None) -> None:
    from ..db.models import TrustedUser

    await ensure_chat(session, chat_id)

    state = await get_member_state(session, chat_id, user_id)
    state.is_trusted = trusted
    state.trust_bypass = list(bypass) if bypass is not None else (list(DEFAULT_TRUST_BYPASS) if trusted else [])
    state.updated_at = datetime.utcnow()
    if trusted:
        result = await session.execute(
            select(TrustedUser).where(TrustedUser.chat_id == chat_id, TrustedUser.user_id == user_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            session.add(TrustedUser(chat_id=chat_id, user_id=user_id,
                                    bypass=list(state.trust_bypass or []),
                                    added_at=datetime.utcnow()))
        else:
            record.bypass = list(state.trust_bypass or [])
    else:
        await session.execute(
            delete(TrustedUser).where(TrustedUser.chat_id == chat_id, TrustedUser.user_id == user_id)
        )
    await session.flush()
    invalidate_member(chat_id, user_id)


async def set_bot_role(session: AsyncSession, chat_id: int, user_id: int, role: str | None,
                       assigned_by: int | None = None) -> None:
    from ..db.models import BotRole

    await ensure_chat(session, chat_id)
    await session.execute(
        delete(BotRole).where(BotRole.chat_id == chat_id, BotRole.user_id == user_id)
    )
    if role:
        session.add(BotRole(chat_id=chat_id, user_id=user_id, role=role,
                            assigned_by=assigned_by, assigned_at=datetime.utcnow()))
    state = await get_member_state(session, chat_id, user_id)
    state.bot_role = role
    state.updated_at = datetime.utcnow()
    await session.flush()
    invalidate_member(chat_id, user_id)


async def touch_activity(session: AsyncSession, chat_id: int, user_id: int, *,
                         message: bool = True, media: bool = False, command: bool = False) -> None:
    """Increment per-chat and per-day activity counters."""
    state = await get_member_state(session, chat_id, user_id)
    now = datetime.utcnow()
    if message:
        state.message_count = int(state.message_count or 0) + 1
    if media:
        state.media_count = int(state.media_count or 0) + 1
    if command:
        state.command_count = int(state.command_count or 0) + 1
    state.last_message_at = now
    state.updated_at = now
    cache.role_cache.delete((chat_id, user_id))


async def set_member_tag(session: AsyncSession, chat_id: int, user_id: int, tag: str | None) -> None:
    state = await get_member_state(session, chat_id, user_id)
    state.tag = (tag or "").strip()[:64] or None
    state.updated_at = datetime.utcnow()
    await session.flush()
    invalidate_member(chat_id, user_id)


async def list_chat_ids(session: AsyncSession, *, active_only: bool = True) -> list[int]:
    query = select(Chat.id)
    if active_only:
        query = query.where(Chat.is_active.is_(True))
    result = await session.execute(query)
    return [row[0] for row in result.all()]


async def prune_old_activity(session: AsyncSession, days: int = 180) -> int:
    from ..db.models import UserActivity

    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await session.execute(delete(UserActivity).where(UserActivity.day < cutoff.date()))
    return int(result.rowcount or 0)


def normalize_trigger(text: str) -> str:
    return normalize_text(text or "", mode="command").strip()
