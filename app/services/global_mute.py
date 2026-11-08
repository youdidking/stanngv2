"""Persistent owner-level global mute across eligible registered groups."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from aiogram import Bot
from aiogram.types import ChatPermissions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core import cache as core_cache
from ..core.errors import safe_delete, safe_restrict, safe_send
from ..db.models import Chat, GlobalMute, User
from .moderation import MUTE_PERMISSIONS, UNMUTE_PERMISSIONS

logger = logging.getLogger("armando.global_mute")


class _Missing:
    pass


_MISSING = _Missing()
_mute_cache = core_cache.TTLCache(maxsize=8192, default_ttl=120.0,
                                  name="global_mute")
_rights_cache = core_cache.TTLCache(maxsize=4096, default_ttl=90.0,
                                    name="global_mute_chat_rights")
core_cache.CACHES.extend((_mute_cache, _rights_cache))


@dataclass(frozen=True)
class MuteSnapshot:
    user_id: int
    chat_ids: tuple[int, ...]
    previous_permissions: dict[str, dict]


def invalidate(user_id: int) -> None:
    _mute_cache.delete(int(user_id))


def invalidate_chat(chat_id: int) -> None:
    _rights_cache.delete(int(chat_id))


_user_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    """Serialize mute/unmute changes for one user within this bot process."""
    key = int(user_id)
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


async def _is_active_in_db(session: AsyncSession, user_id: int) -> bool:
    value = await session.scalar(
        select(GlobalMute.active).where(GlobalMute.user_id == int(user_id)).limit(1))
    return bool(value)


async def get_record(session: AsyncSession, user_id: int) -> GlobalMute | None:
    record = await session.get(GlobalMute, int(user_id))
    return record if record is not None else None


async def is_muted(session: AsyncSession, user_id: int) -> str | None:
    """Return the saved reason for an active global mute, otherwise ``None``."""
    key = int(user_id)
    cached = _mute_cache.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    result = await session.execute(
        select(GlobalMute.active, GlobalMute.reason)
        .where(GlobalMute.user_id == key).limit(1))
    row = result.first()
    reason = (row[1] or "بدون دلیل") if row is not None and bool(row[0]) else None
    _mute_cache.set(key, reason)
    return reason


async def activate(session: AsyncSession, *, user_id: int, muted_by: int,
                   reason: str) -> GlobalMute:
    """Create/update the persistent mute without losing owned per-chat state."""
    user_id = int(user_id)
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, first_name=""))
        await session.flush()
    record = await get_record(session, user_id)
    if record is None:
        record = GlobalMute(
            user_id=user_id, muted_by=int(muted_by), reason=(reason or "بدون دلیل")[:300],
            active=True, applied_chat_ids=[], previous_permissions={},
            created_at=datetime.utcnow())
        session.add(record)
    else:
        record.muted_by = int(muted_by)
        record.reason = (reason or "بدون دلیل")[:300]
        record.active = True
        record.created_at = datetime.utcnow()
        if record.applied_chat_ids is None:
            record.applied_chat_ids = []
        if record.previous_permissions is None:
            record.previous_permissions = {}
    invalidate(user_id)
    await session.flush()
    return record


def _snapshot(record: GlobalMute) -> MuteSnapshot:
    return MuteSnapshot(
        user_id=int(record.user_id),
        chat_ids=tuple(sorted({int(item) for item in (record.applied_chat_ids or [])})),
        previous_permissions=dict(record.previous_permissions or {}),
    )


async def deactivate(session: AsyncSession, *, user_id: int) -> MuteSnapshot | None:
    """Mark the global mute off and return the exact restrictions to restore."""
    record = await get_record(session, user_id)
    if record is None:
        return None
    snapshot = _snapshot(record)
    if not record.active and not snapshot.chat_ids:
        return None
    record.active = False
    invalidate(user_id)
    await session.flush()
    return snapshot


async def _record_applied(session: AsyncSession, *, user_id: int, chat_id: int,
                          previous: dict) -> None:
    record = await get_record(session, user_id)
    if record is None:
        return
    applied = {int(item) for item in (record.applied_chat_ids or [])}
    applied.add(int(chat_id))
    record.applied_chat_ids = sorted(applied)
    previous_map = dict(record.previous_permissions or {})
    key = str(int(chat_id))
    # Reapplication only reaches here when the user was speakable again.
    # That may be a newer local unmute/restriction, so preserve this fresh
    # pre-global-mute state rather than restoring a stale earlier snapshot.
    previous_map[key] = previous
    record.previous_permissions = previous_map
    await session.flush()


async def _chat_is_eligible(bot: Bot, chat_id: int) -> bool:
    """Require a present bot admin with restrict rights and the owner in-chat."""
    key = int(chat_id)
    cached = _rights_cache.get(key, _MISSING)
    if cached is not _MISSING:
        return bool(cached)
    owner_id = int(settings.owner_id or 0)
    if owner_id <= 0:
        _rights_cache.set(key, False)
        return False
    try:
        bot_member = await bot.get_chat_member(chat_id=key, user_id=int(bot.id))
        bot_status = str(getattr(bot_member, "status", ""))
        bot_can_restrict = (bot_status == "creator"
                            or (bot_status == "administrator"
                                and bool(getattr(bot_member, "can_restrict_members", False))))
        if not bot_can_restrict:
            _rights_cache.set(key, False)
            return False

        # The owner's being a regular member is enough. Requiring the owner to
        # be an administrator was stricter than global ban and wrongly skipped
        # groups where the owner was present but not managing the group.
        owner_member = await bot.get_chat_member(chat_id=key, user_id=owner_id)
        owner_status = str(getattr(owner_member, "status", ""))
        owner_present = owner_status in {"creator", "administrator", "member"}
        if owner_status == "restricted":
            owner_present = bool(getattr(owner_member, "is_member", False))
        allowed = owner_present
    except Exception as exc:  # noqa: BLE001 - fail closed when the status is unknown
        logger.debug("global mute rights unknown chat=%s: %s", key,
                     exc.__class__.__name__)
        allowed = False
    _rights_cache.set(key, allowed)
    return allowed


async def _previous_state(bot: Bot, chat_id: int, member) -> dict:
    """Serialize the prior state, including group defaults for ordinary members."""
    if str(getattr(member, "status", "")) != "restricted":
        try:
            chat = await bot.get_chat(chat_id=int(chat_id))
            default_permissions = getattr(chat, "permissions", None)
            if default_permissions is not None:
                default_data = default_permissions.model_dump(exclude_none=True)
                # Missing permission fields should retain the normal member
                # baseline instead of accidentally becoming additional denies.
                baseline = UNMUTE_PERMISSIONS.model_dump(exclude_none=True)
                baseline.update(default_data)
                return {"was_restricted": False, "permissions": baseline}
        except Exception as exc:  # noqa: BLE001 - keep a safe fallback if unavailable
            logger.debug("global mute defaults unknown chat=%s: %s", chat_id,
                         exc.__class__.__name__)
        return {"was_restricted": False}
    permissions = getattr(member, "permissions", None)
    if permissions is None:
        dumped: dict = {}
    else:
        try:
            dumped = permissions.model_dump(exclude_none=True)
        except Exception:  # noqa: BLE001 - support simple test doubles and older models
            try:
                dumped = dict(permissions)
            except Exception:  # noqa: BLE001
                dumped = {}
    until_date = getattr(member, "until_date", None)
    if hasattr(until_date, "isoformat"):
        until_date = until_date.isoformat()
    else:
        until_date = None
    return {"was_restricted": True, "permissions": dumped,
            "until_date": until_date}


async def _mute_in_chat_locked(bot: Bot, session: AsyncSession, *, chat_id: int,
                               user_id: int) -> bool:
    """Apply one restriction; the caller holds this user's rollout lock."""
    chat_id, user_id = int(chat_id), int(user_id)
    chat_type = await session.scalar(
        select(Chat.type).where(Chat.id == chat_id, Chat.is_active.is_(True)).limit(1))
    if chat_type != "supergroup":
        return False
    if not await _is_active_in_db(session, user_id):
        return False
    if not await _chat_is_eligible(bot, chat_id):
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - fail closed for unknown target status
        logger.debug("global mute target status unknown chat=%s user=%s: %s",
                     chat_id, user_id, exc.__class__.__name__)
        return False
    status = str(getattr(member, "status", ""))
    if status in {"administrator", "creator", "left", "kicked"}:
        return False
    if status not in {"member", "restricted"}:
        return False
    if status == "restricted":
        current_permissions = getattr(member, "permissions", None)
        if (current_permissions is not None
                and getattr(current_permissions, "can_send_messages", None) is False):
            # Already unable to speak; leave the other restriction untouched.
            return False
    # Recheck immediately before the Bot API write; activation may have been
    # cancelled while we were fetching chat/member details.
    if not await _is_active_in_db(session, user_id):
        return False
    previous = await _previous_state(bot, chat_id, member)
    restricted = await safe_restrict(
        bot, chat_id, user_id, MUTE_PERMISSIONS, context="global_mute")
    if restricted:
        await _record_applied(session, user_id=user_id, chat_id=chat_id,
                              previous=previous)
    return restricted


async def mute_in_chat(bot: Bot, session: AsyncSession, *, chat_id: int,
                       user_id: int) -> bool:
    """Restrict one member under a per-user lock, only while mute is active."""
    user_id = int(user_id)
    async with user_lock(user_id):
        return await _mute_in_chat_locked(
            bot, session, chat_id=int(chat_id), user_id=user_id)


async def unmute_in_chat(bot: Bot, chat_id: int, user_id: int,
                         previous: dict | None = None) -> bool:
    """Restore only if the current restriction still matches our global mute.

    If a local admin changed the restriction while the global mute was active,
    leave that newer state alone instead of undoing their moderation.
    """
    try:
        member = await bot.get_chat_member(chat_id=int(chat_id), user_id=int(user_id))
    except Exception as exc:  # noqa: BLE001 - retry on the next unmute if unknown
        logger.debug("global unmute status unknown chat=%s user=%s: %s",
                     chat_id, user_id, exc.__class__.__name__)
        return False
    status = str(getattr(member, "status", ""))
    if status in {"member", "left", "kicked", "administrator", "creator"}:
        # It was already released or the user is no longer restrictable.
        return True
    if status != "restricted":
        return False
    current_permissions = getattr(member, "permissions", None)
    if current_permissions is None:
        return False
    try:
        current_data = current_permissions.model_dump(exclude_none=True)
        global_data = MUTE_PERMISSIONS.model_dump(exclude_none=True)
    except Exception:  # noqa: BLE001
        return False
    if not all(current_data.get(key) == value for key, value in global_data.items()):
        return True  # newer/other local restriction: preserve it and forget our layer

    previous = previous or {}
    if previous.get("was_restricted"):
        permissions_data = previous.get("permissions") or {}
        permissions = ChatPermissions(**permissions_data)
        raw_until = previous.get("until_date")
        until_date = None
        if raw_until:
            try:
                until_date = datetime.fromisoformat(str(raw_until))
            except (TypeError, ValueError):
                until_date = None
    elif previous.get("permissions"):
        # Ordinary members inherit the group's default permissions. Restore
        # those explicit defaults when they were available at apply time.
        permissions = ChatPermissions(**previous["permissions"])
        until_date = None
    else:
        permissions = UNMUTE_PERMISSIONS
        until_date = None
    return await safe_restrict(
        bot, int(chat_id), int(user_id), permissions, until_date=until_date,
        context="global_unmute")


async def known_chat_ids(session: AsyncSession) -> list[int]:
    """Use exactly the same active-chat registry as global ban (groups/channels)."""
    from .gban import guarded_chat_ids

    return sorted(await guarded_chat_ids(session))


async def known_group_chat_ids(session: AsyncSession) -> list[int]:
    """Active registered supergroups; Telegram only supports member restrictions there."""
    result = await session.execute(
        select(Chat.id).where(Chat.is_active.is_(True), Chat.type == "supergroup")
        .order_by(Chat.id))
    return [int(row[0]) for row in result.all()]


async def apply_everywhere(bot: Bot, session: AsyncSession, user_id: int, *,
                           chat_ids: Iterable[int] | None = None,
                           delay: float = 0.15) -> int:
    """Apply an active mute to every eligible, registered supergroup.

    Commit after each candidate so a concurrent lift is visible before the next
    group, and stop immediately once the persistent mute has been switched off.
    """
    user_id = int(user_id)
    # Enumerate the same persistent registry as global ban (including channels),
    # then restrict actual group/supergroup targets only. A linked discussion
    # group is eligible in its own right once it has been registered.
    ids = list(chat_ids) if chat_ids is not None else await known_chat_ids(session)
    group_ids = set(await known_group_chat_ids(session))
    done = 0
    for index, chat_id in enumerate(ids):
        chat_id = int(chat_id)
        if chat_id not in group_ids:
            continue
        async with user_lock(user_id):
            if not await _is_active_in_db(session, user_id):
                break
            if await _mute_in_chat_locked(
                    bot, session, chat_id=chat_id, user_id=user_id):
                done += 1
            # Commit while still holding the lock so a simultaneous lift sees
            # this chat in its cleanup snapshot before it changes active state.
            await session.commit()
        if delay and index + 1 < len(ids):
            await asyncio.sleep(delay)
    return done


async def enforce(bot: Bot, session: AsyncSession, *, chat_id: int, user_id: int,
                  user_name: str = "", reason: str | None = None,
                  message_id: int | None = None, notify: bool = False) -> bool:
    """Enforce an active global mute on a newly joined or speaking member."""
    active_reason = reason if reason is not None else await is_muted(session, user_id)
    if active_reason is None:
        return False
    user_id = int(user_id)
    async with user_lock(user_id):
        applied = await _mute_in_chat_locked(
            bot, session, chat_id=int(chat_id), user_id=user_id)
        if applied:
            # An unmute transaction must see every successful enforcement before
            # it snapshots the applied-chat list.
            await session.commit()
    if not applied:
        return False
    if message_id is not None:
        await safe_delete(bot, chat_id, message_id, context="global_mute_cleanup")
    if notify:
        await safe_send(
            bot, chat_id,
            "🔇 <b>سکوت سراسری</b>\n\n"
            f"<a href=\"tg://user?id={user_id}\">{html.escape(user_name or str(user_id))}</a> "
            "در فهرست سکوت سراسری ربات است و در این گروه محدود شد.\n"
            f"📌 دلیل: {html.escape(active_reason or 'بدون دلیل')}",
            parse_mode="HTML")
    logger.info("global mute enforced user=%s chat=%s", user_id, chat_id)
    return True


async def lift_everywhere(bot: Bot, session: AsyncSession, snapshot: MuteSnapshot, *,
                          delay: float = 0.15) -> int:
    """Undo only per-chat restrictions created by the global-mute feature."""
    user_id = int(snapshot.user_id)
    async with user_lock(user_id):
        # The caller's snapshot can predate an in-flight application. Refresh it
        # after acquiring the lock so a just-finished mute is also lifted.
        record = await get_record(session, user_id)
        if record is not None and record.active:
            # A newer activation won the lock before this stale lift task; keep
            # the mute restrictions in place for the active mute.
            return 0
        if record is not None:
            snapshot = _snapshot(record)
        done = 0
        succeeded: set[int] = set()
        chat_ids = tuple(snapshot.chat_ids)
        for index, chat_id in enumerate(chat_ids):
            previous = snapshot.previous_permissions.get(str(chat_id), {})
            if await unmute_in_chat(bot, chat_id, user_id, previous):
                done += 1
                succeeded.add(int(chat_id))
            if delay and index + 1 < len(chat_ids):
                await asyncio.sleep(delay)

        # Retain only failed cleanups so a repeated lift can retry without later
        # undoing a local restriction newly applied in a successful chat.
        record = await get_record(session, user_id)
        if record is not None and not record.active:
            remaining = [int(chat_id) for chat_id in (record.applied_chat_ids or [])
                         if int(chat_id) not in succeeded]
            record.applied_chat_ids = remaining
            previous_map = dict(record.previous_permissions or {})
            remaining_set = set(remaining)
            record.previous_permissions = {
                key: value for key, value in previous_map.items()
                if int(key) in remaining_set
            }
            # Commit before releasing the lock; a new activation/application
            # must not race a stale cleanup-list write.
            await session.commit()
        return done
