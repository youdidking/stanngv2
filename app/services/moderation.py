"""Core moderation engine: ban / kick / mute / warn and the shared action engine.

Every action writes a :class:`ModerationAction` record, an :class:`AuditLog`
entry and (optionally) mirrors itself to the group log channel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.types import ChatPermissions
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.duration import format_duration
from ..core.errors import safe_ban, safe_delete, safe_restrict, safe_unban
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_datetime
from ..db.models import (
    BanRecord,
    ModerationAction,
    User,
    Warning,
)
from ..db.models import ChatMemberState
from .audit import audit_and_log, html_user
from .chat_state import get_member_state, get_settings, invalidate_member
from .permissions import Actor, build_actor, check_moderation_target

logger = logging.getLogger("armando.moderation")

MUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
    can_manage_topics=False,
)

ACTION_LABELS_FA = {
    "ban": "🔨 بن",
    "temp_ban": "⏳ بن موقت",
    "unban": "🕊 رفع بن",
    "kick": "👢 اخراج",
    "mute": "🔇 سکوت",
    "temp_mute": "⏳ سکوت موقت",
    "unmute": "🔊 لغو سکوت",
    "warn": "⚠️ اخطار",
    "unwarn": "🗑 کسر اخطار",
    "reset_warn": "♻️ صفر کردن اخطارها",
    "delete": "🗑 حذف پیام",
    "none": "بدون اقدام",
}

HISTORY_SOURCE_LABELS_FA = {
    "auto": "خودکار",
    "command": "دستور",
    "panel": "پنل",
    "lock": "قفل",
    "filter": "فیلتر",
    "antiflood": "ضدسیل",
    "antispam": "ضداسپم",
    "ai": "هوش مصنوعی",
    "nightmode": "حالت شب",
    "captcha": "کپچا",
    "warn_threshold": "سقف اخطار",
    "federation": "فدراسیون",
    "warning": "سیستم اخطار",
}

PUNISHMENT_OPTIONS_FA = {
    "none": "بدون اقدام",
    "delete": "حذف پیام",
    "warn": "اخطار",
    "mute": "سکوت",
    "temp_mute": "سکوت موقت",
    "kick": "اخراج",
    "ban": "بن",
    "temp_ban": "بن موقت",
    "delete_warn": "حذف + اخطار",
    "delete_mute": "حذف + سکوت",
    "delete_ban": "حذف + بن",
    "delete_kick": "حذف + اخراج",
}


def action_label_fa(action: str) -> str:
    return ACTION_LABELS_FA.get(action, action)


async def _display_name(session: AsyncSession, user_id: int | None) -> str:
    if not user_id:
        return "—"
    user = await session.get(User, user_id)
    if user:
        return f"{user.first_name or ''} {user.last_name or ''}".strip() or f"@{user.username or user_id}"
    return str(user_id)


def mention(user_id: int | None, name: str) -> str:
    return html_user(user_id, name)


# --------------------------------------------------------------------------- #
# Record keeping
# --------------------------------------------------------------------------- #
async def record_action(session: AsyncSession, *, chat_id: int, action: str, target_id: int,
                        actor_id: int | None, reason: str = "", duration: int | None = None,
                        source: str = "command", message_id: int | None = None,
                        payload: dict | None = None) -> ModerationAction:
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=duration) if duration else None
    entry = ModerationAction(
        chat_id=chat_id,
        target_id=target_id,
        actor_id=actor_id,
        action=action,
        reason=(reason or "")[:500],
        duration=duration,
        expires_at=expires_at,
        active=True,
        source=source,
        message_id=message_id,
        payload=payload or {},
        created_at=now,
    )
    session.add(entry)
    await session.flush()
    return entry


async def close_active_actions(session: AsyncSession, chat_id: int, target_id: int,
                               action: str, undone_by: int | None = None) -> int:
    result = await session.execute(
        update(ModerationAction)
        .where(ModerationAction.chat_id == chat_id,
               ModerationAction.target_id == target_id,
               ModerationAction.action == action,
               ModerationAction.active.is_(True))
        .values(active=False, undone_at=datetime.utcnow(), undone_by=undone_by)
    )
    return int(result.rowcount or 0)


async def _manual_action_error(bot: Bot, session: AsyncSession, *, chat_id: int,
                               actor: Actor, target_id: int,
                               protected_actor_ids: list[int | None] | None = None,
                               allow_absent: bool = False) -> str:
    """Fail closed for self-targeting, admin targets, and owner-issued actions."""
    reason = await check_moderation_target(
        bot, session, chat_id, target_id, actor, allow_absent=allow_absent)
    if reason:
        return reason
    if actor.is_group_creator:
        return ""

    for issuer_id in set(protected_actor_ids or []):
        if not issuer_id or issuer_id in {actor.user_id, bot.id}:
            continue
        issuer = await build_actor(bot, chat_id, issuer_id, session=session)
        if issuer.is_group_creator:
            return "⛔️ این اقدام را مالک گروه انجام داده است؛ فقط مالک گروه می‌تواند آن را لغو کند."
    return ""


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
async def ban_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                  target_id: int, target_name: str = "", duration: int | None = None,
                  reason: str = "", delete_message_id: int | None = None,
                  chat_title: str = "", source: str = "command") -> str:
    if source in {"command", "panel", "multi_ban"}:
        denied = await _manual_action_error(bot, session, chat_id=chat_id,
                                            actor=actor, target_id=target_id)
        if denied:
            return denied
    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None
    ok = await safe_ban(bot, chat_id, target_id, until_date=until, context="ban")
    if not ok:
        # Never create an active ban record when Telegram rejected the ban.
        return "⚠️ عملیات در تلگرام انجام نشد (احتمالاً دسترسی ربات ناکافی است)."

    action = "temp_ban" if duration else "ban"
    await record_action(session, chat_id=chat_id, action=action, target_id=target_id,
                        actor_id=actor.user_id, reason=reason, duration=duration, source=source)
    state = await get_member_state(session, chat_id, target_id)
    state.status = "kicked"
    state.restricted_until = until
    state.updated_at = datetime.utcnow()
    session.add(BanRecord(chat_id=chat_id, user_id=target_id, banned_by=actor.user_id,
                          reason=(reason or "")[:500], scope="chat", active=True, until=until,
                          created_at=datetime.utcnow()))
    invalidate_member(chat_id, target_id)

    if delete_message_id:
        await safe_delete(bot, chat_id, delete_message_id, context="ban_delete")

    await audit_and_log(
        session, bot, chat_id=chat_id, action=action, actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason,
        duration=format_duration(duration) if duration else "",
    )
    from .ban_reports import report_successful_ban

    await report_successful_ban(
        bot, session, chat_id=chat_id, target_id=target_id,
        actor_id=actor.user_id, reason=reason, duration=duration, source=source,
        chat_title=chat_title,
        target_name=target_name or await _display_name(session, target_id),
        actor_name=await _display_name(session, actor.user_id),
        action=action,
    )
    text = (f"🔨 کاربر {mention(target_id, target_name or await _display_name(session, target_id))} "
            f"{'به‌مدت ' + format_duration(duration) + ' ' if duration else ''}بن شد.")
    if reason:
        text += f"\n📌 دلیل: {reason}"
    return text


async def unban_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                     target_id: int, target_name: str = "", chat_title: str = "",
                     reason: str = "", source: str = "command") -> str:
    action_rows = (await session.execute(
        select(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_id == target_id,
            ModerationAction.action.in_(("ban", "temp_ban", "kick")),
            ModerationAction.active.is_(True),
        )
    )).scalars().all()
    now = datetime.utcnow()
    protected_ids = [item.actor_id for item in action_rows
                     if item.expires_at is None or item.expires_at > now]
    ban_rows = (await session.execute(
        select(BanRecord).where(
            BanRecord.chat_id == chat_id, BanRecord.user_id == target_id,
            BanRecord.scope == "chat", BanRecord.active.is_(True),
        )
    )).scalars().all()
    protected_ids.extend(record.banned_by for record in ban_rows
                         if record.until is None or record.until > now)
    denied = await _manual_action_error(
        bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
        protected_actor_ids=protected_ids, allow_absent=True)
    if denied:
        return denied

    ok = await safe_unban(bot, chat_id, target_id, only_if_banned=True, context="unban")
    await close_active_actions(session, chat_id, target_id, "ban", undone_by=actor.user_id)
    await close_active_actions(session, chat_id, target_id, "temp_ban", undone_by=actor.user_id)
    await close_active_actions(session, chat_id, target_id, "kick", undone_by=actor.user_id)
    await record_action(session, chat_id=chat_id, action="unban", target_id=target_id,
                        actor_id=actor.user_id, reason=reason, source=source)
    state = await get_member_state(session, chat_id, target_id)
    state.status = "member"
    state.restricted_until = None
    state.updated_at = datetime.utcnow()
    await session.execute(
        update(BanRecord).where(BanRecord.chat_id == chat_id, BanRecord.user_id == target_id)
        .values(active=False)
    )
    invalidate_member(chat_id, target_id)
    await audit_and_log(
        session, bot, chat_id=chat_id, action="unban", actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason,
    )
    if not ok:
        return "⚠️ کاربر در حال حاضر بن نبود یا تلگرام اجازه این کار را نداد."
    return (f"🕊 بن کاربر {mention(target_id, target_name or await _display_name(session, target_id))} "
            f"برداشته شد.")


async def kick_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                    target_id: int, target_name: str = "", reason: str = "",
                    delete_message_id: int | None = None, chat_title: str = "",
                    source: str = "command", sticky: bool = True) -> str:
    """Remove a member from the chat.

    ``sticky=True`` (the default, driven by the ``sticky_ban`` setting) keeps
    the ban in place, so the member can **never** come back through an invite
    link until an admin unbans them.  ``sticky=False`` performs the classic
    kick - ban followed by an immediate unban - which lets the member re-join.
    """
    if source in {"command", "panel"}:
        denied = await _manual_action_error(bot, session, chat_id=chat_id,
                                            actor=actor, target_id=target_id)
        if denied:
            return denied
    ok = await safe_ban(bot, chat_id, target_id, context="kick")
    if not ok:
        return "⚠️ اخراج در تلگرام انجام نشد (احتمالاً دسترسی ربات ناکافی است)."
    unbanned = True
    if sticky:
        # The ban stays: Telegram then blocks every re-entry attempt.
        session.add(BanRecord(chat_id=chat_id, user_id=target_id, banned_by=actor.user_id,
                              reason=(reason or "")[:500], scope="chat", active=True,
                              until=None, created_at=datetime.utcnow()))
    else:
        unbanned = await safe_unban(bot, chat_id, target_id,
                                    only_if_banned=False, context="kick_unban")
        if not unbanned:
            # The user was removed but Telegram did not lift the ban.
            session.add(BanRecord(chat_id=chat_id, user_id=target_id, banned_by=actor.user_id,
                                  reason=(reason or "")[:500], scope="chat", active=True,
                                  until=None, created_at=datetime.utcnow()))
    await record_action(session, chat_id=chat_id, action="kick", target_id=target_id,
                        actor_id=actor.user_id, reason=reason, source=source)
    state = await get_member_state(session, chat_id, target_id)
    state.status = "kicked" if (sticky or not unbanned) else "left"
    state.updated_at = datetime.utcnow()
    invalidate_member(chat_id, target_id)
    if delete_message_id:
        await safe_delete(bot, chat_id, delete_message_id, context="kick_delete")
    await audit_and_log(
        session, bot, chat_id=chat_id, action="kick", actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason,
    )
    if sticky or not unbanned:
        from .ban_reports import report_successful_ban

        await report_successful_ban(
            bot, session, chat_id=chat_id, target_id=target_id,
            actor_id=actor.user_id, reason=reason, source=source,
            chat_title=chat_title,
            target_name=target_name or await _display_name(session, target_id),
            actor_name=await _display_name(session, actor.user_id), action="kick")
    who = mention(target_id, target_name or await _display_name(session, target_id))
    if sticky:
        return (f"👢 کاربر {who} از گروه اخراج شد و بن ماندگار شد.\n"
                "🔒 در سوپرگروه، تلگرام تا رفع‌بن توسط مدیر جلوی ورود دوباره با لینک را می‌گیرد؛ "
                "در گروه معمولی این تضمین وجود ندارد.")
    if not unbanned:
        return f"⚠️ کاربر {who} اخراج شد، اما تلگرام رفع بن را نپذیرفت؛ او همچنان بن است."
    return (f"👢 کاربر {who} از گروه اخراج شد.")


async def enforce_sticky_ban(bot: Bot, session: AsyncSession, chat_id: int,
                             user_id: int) -> bool:
    """Re-ban a member who is still banned but somehow got back in.

    Telegram normally blocks a banned user from re-entering a supergroup, but
    basic groups, admin re-adds and invite-link edge cases can slip through.
    Returns True when the member was removed again.
    """
    from sqlalchemy import select as _select

    record = (await session.execute(
        _select(BanRecord).where(BanRecord.chat_id == chat_id,
                                 BanRecord.user_id == user_id,
                                 BanRecord.active.is_(True)))).scalars().first()
    if record is None:
        return False
    if record.until is not None and record.until <= datetime.utcnow():
        record.active = False
        return False
    removed = await safe_ban(bot, chat_id, user_id, context="sticky_reban")
    if removed:
        logger.info("sticky ban re-applied user=%s chat=%s", user_id, chat_id)
        from .ban_reports import report_successful_ban

        await report_successful_ban(
            bot, session, chat_id=chat_id, target_id=user_id, actor_id=bot.id,
            reason=record.reason or "اجرای دوبارهٔ بن فعال", source="sticky_reban",
            target_name=await _display_name(session, user_id))
    return bool(removed)


async def has_active_ban(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """True when ``user_id`` is still banned in ``chat_id``.

    Used to close the *join request* entry path: a banned member must not be
    waved back in by an admin (or by the auto-approve rule) while the ban
    stands.  An expired temporary ban is treated as no ban at all.
    """
    from sqlalchemy import select as _select

    record = (await session.execute(
        _select(BanRecord).where(BanRecord.chat_id == chat_id,
                                 BanRecord.user_id == user_id,
                                 BanRecord.active.is_(True)))).scalars().first()
    if record is None:
        return False
    if record.until is not None and record.until <= datetime.utcnow():
        record.active = False
        return False
    return True


async def mute_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                    target_id: int, target_name: str = "", duration: int | None = None,
                    reason: str = "", delete_message_id: int | None = None,
                    chat_title: str = "", source: str = "command") -> str:
    if source in {"command", "panel"}:
        denied = await _manual_action_error(bot, session, chat_id=chat_id,
                                            actor=actor, target_id=target_id)
        if denied:
            return denied
    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None
    ok = await safe_restrict(bot, chat_id, target_id, MUTE_PERMISSIONS, until_date=until,
                             context="mute")
    action = "temp_mute" if duration else "mute"
    await close_active_actions(session, chat_id, target_id, "mute", undone_by=actor.user_id)
    await close_active_actions(session, chat_id, target_id, "temp_mute", undone_by=actor.user_id)
    await record_action(session, chat_id=chat_id, action=action, target_id=target_id,
                        actor_id=actor.user_id, reason=reason, duration=duration, source=source)
    state = await get_member_state(session, chat_id, target_id)
    state.muted_until = until
    state.status = "restricted"
    state.updated_at = datetime.utcnow()
    invalidate_member(chat_id, target_id)
    if delete_message_id:
        await safe_delete(bot, chat_id, delete_message_id, context="mute_delete")
    await audit_and_log(
        session, bot, chat_id=chat_id, action=action, actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason,
        duration=format_duration(duration) if duration else "",
    )
    if not ok:
        return "⚠️ عملیات در تلگرام انجام نشد (احتمالاً دسترسی ربات ناکافی است)."
    return (f"🔇 کاربر {mention(target_id, target_name or await _display_name(session, target_id))} "
            f"{'به‌مدت ' + format_duration(duration) + ' ' if duration else ''}بی‌صدا شد.")


async def unmute_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                      target_id: int, target_name: str = "", chat_title: str = "",
                      reason: str = "", source: str = "command") -> str:
    action_rows = (await session.execute(
        select(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            ModerationAction.target_id == target_id,
            ModerationAction.action.in_(("mute", "temp_mute")),
            ModerationAction.active.is_(True),
        )
    )).scalars().all()
    now = datetime.utcnow()
    protected_ids = [item.actor_id for item in action_rows
                     if item.expires_at is None or item.expires_at > now]
    denied = await _manual_action_error(
        bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
        protected_actor_ids=protected_ids)
    if denied:
        return denied

    ok = await safe_restrict(bot, chat_id, target_id, UNMUTE_PERMISSIONS, context="unmute")
    await close_active_actions(session, chat_id, target_id, "mute", undone_by=actor.user_id)
    await close_active_actions(session, chat_id, target_id, "temp_mute", undone_by=actor.user_id)
    await record_action(session, chat_id=chat_id, action="unmute", target_id=target_id,
                        actor_id=actor.user_id, reason=reason, source=source)
    state = await get_member_state(session, chat_id, target_id)
    state.muted_until = None
    state.status = "member"
    state.updated_at = datetime.utcnow()
    invalidate_member(chat_id, target_id)
    await audit_and_log(
        session, bot, chat_id=chat_id, action="unmute", actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason,
    )
    if not ok:
        return "⚠️ عملیات در تلگرام انجام نشد (احتمالاً دسترسی ربات ناکافی است)."
    return (f"🔊 سکوت کاربر {mention(target_id, target_name or await _display_name(session, target_id))} "
            f"برداشته شد.")


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #
async def warn_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                    target_id: int, target_name: str = "", reason: str = "",
                    delete_message_id: int | None = None, chat_title: str = "",
                    source: str = "command") -> tuple[str, dict[str, Any]]:
    """Add a warning and execute the threshold action when the limit is hit."""
    if source in {"command", "panel"}:
        denied = await _manual_action_error(bot, session, chat_id=chat_id,
                                            actor=actor, target_id=target_id)
        if denied:
            return denied, {"denied": True}
    settings_obj = await get_settings(session, chat_id)
    warn_limit = int(settings_obj.warn_limit or 4)
    expires_at = None
    if int(settings_obj.warn_expire_days or 0) > 0:
        expires_at = datetime.utcnow() + timedelta(days=int(settings_obj.warn_expire_days))

    session.add(Warning(chat_id=chat_id, user_id=target_id, moderator_id=actor.user_id,
                        reason=(reason or "بدون دلیل")[:500], created_at=datetime.utcnow(),
                        expires_at=expires_at))
    await record_action(session, chat_id=chat_id, action="warn", target_id=target_id,
                        actor_id=actor.user_id, reason=reason, source=source)

    state = await get_member_state(session, chat_id, target_id)
    state.warn_count = int(state.warn_count or 0) + 1
    state.updated_at = datetime.utcnow()
    invalidate_member(chat_id, target_id)

    count = int(state.warn_count or 0)
    threshold_reached = count >= warn_limit

    if delete_message_id:
        await safe_delete(bot, chat_id, delete_message_id, context="warn_delete")

    await audit_and_log(
        session, bot, chat_id=chat_id, action="warn", actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title, reason=reason, extra=f"🔢 تعداد اخطار: {count} از {warn_limit}",
    )

    result: dict[str, Any] = {"count": count, "limit": warn_limit, "threshold": threshold_reached,
                              "action": "none"}
    if not threshold_reached:
        text = (f"⚠️ به {mention(target_id, target_name or await _display_name(session, target_id))} "
                f"اخطار داده شد.\n🔢 تعداد اخطار: {count} از {warn_limit}")
        if reason:
            text += f"\n📌 دلیل: {reason}"
        return text, result

    # Threshold reached -> apply the configured action.
    from .warnings import threshold_action

    action = settings_obj.warn_action or "mute"
    duration = settings_obj.warn_action_duration
    text, applied = await threshold_action(
        bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
        target_name=target_name, action=action, duration=duration,
        reason=f"رسیدن به سقف اخطار ({count}/{warn_limit})",
        chat_title=chat_title,
    )
    result["action"] = applied
    await reset_warnings(session, chat_id, target_id, by_id=actor.user_id, auto=True)
    return text, result


async def unwarn_user(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                      target_id: int, target_name: str = "", chat_title: str = "") -> str:
    result = await session.execute(
        select(Warning)
        .where(Warning.chat_id == chat_id, Warning.user_id == target_id, Warning.active.is_(True))
        .order_by(Warning.created_at.desc())
        .limit(1)
    )
    warning = result.scalars().first()
    if warning is None:
        denied = await _manual_action_error(
            bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
            allow_absent=True)
        return denied or "ℹ️ این کاربر اخطار فعالی ندارد."
    denied = await _manual_action_error(
        bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
        protected_actor_ids=[warning.moderator_id], allow_absent=True)
    if denied:
        return denied
    warning.active = False
    warning.removed_at = datetime.utcnow()
    warning.removed_by = actor.user_id
    state = await get_member_state(session, chat_id, target_id)
    state.warn_count = max(0, int(state.warn_count or 0) - 1)
    state.updated_at = datetime.utcnow()
    await record_action(session, chat_id=chat_id, action="unwarn", target_id=target_id,
                        actor_id=actor.user_id, reason="", source="command")
    invalidate_member(chat_id, target_id)
    await audit_and_log(
        session, bot, chat_id=chat_id, action="unwarn", actor_id=actor.user_id, target_id=target_id,
        actor_name=mention(actor.user_id, await _display_name(session, actor.user_id)),
        target_name=mention(target_id, target_name or await _display_name(session, target_id)),
        chat_title=chat_title,
    )
    return (f"🗑 یک اخطار از {mention(target_id, target_name or await _display_name(session, target_id))} "
            f"کم شد.\n🔢 تعداد فعلی: {state.warn_count}")


async def reset_warnings(session: AsyncSession, chat_id: int, user_id: int, *,
                         by_id: int | None = None, auto: bool = False,
                         bot: Bot | None = None, actor: Actor | None = None) -> int | str:
    result = await session.execute(
        select(Warning).where(Warning.chat_id == chat_id, Warning.user_id == user_id,
                              Warning.active.is_(True))
    )
    warnings = list(result.scalars().all())
    if not auto:
        if bot is None or actor is None:
            return "⛔️ برای پاک‌کردن اخطارها، هویت و دسترسی مدیر باید دوباره بررسی شود."
        denied = await _manual_action_error(
            bot, session, chat_id=chat_id, actor=actor, target_id=user_id,
            protected_actor_ids=[warning.moderator_id for warning in warnings],
            allow_absent=True)
        if denied:
            return denied

    now = datetime.utcnow()
    for warning in warnings:
        warning.active = False
        warning.removed_at = now
        warning.removed_by = actor.user_id if actor is not None else by_id
    count = len(warnings)
    state = await get_member_state(session, chat_id, user_id)
    state.warn_count = 0
    state.updated_at = now
    await session.flush()
    invalidate_member(chat_id, user_id)
    if count:
        session.add(ModerationAction(
            chat_id=chat_id, target_id=user_id,
            actor_id=actor.user_id if actor is not None else by_id,
            action="reset_warn", reason="", created_at=now,
            active=False, source="auto" if auto else "command"))
    return count


async def warn_status(session: AsyncSession, chat_id: int, user_id: int) -> tuple[int, int, list[Warning]]:
    settings_obj = await get_settings(session, chat_id)
    limit = int(settings_obj.warn_limit or 4)
    result = await session.execute(
        select(Warning).where(Warning.chat_id == chat_id, Warning.user_id == user_id)
        .order_by(Warning.created_at.desc()).limit(20)
    )
    records = list(result.scalars().all())
    state = await get_member_state(session, chat_id, user_id, create=False)
    count = int(state.warn_count or 0) if state else sum(1 for w in records if w.active)
    return count, limit, records


async def active_warn_count(session: AsyncSession, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(func.count(Warning.id)).where(Warning.chat_id == chat_id, Warning.user_id == user_id,
                                             Warning.active.is_(True))
    )
    return int(result.scalar() or 0)


# --------------------------------------------------------------------------- #
# Shared action engine (used by locks / filters / antiflood / warnings)
# --------------------------------------------------------------------------- #
async def apply_action(bot: Bot, session: AsyncSession, *, chat_id: int, target_id: int,
                       actor: Actor | None, action: str, reason: str = "",
                       duration: int | None = None, target_name: str = "",
                       message_id: int | None = None, chat_title: str = "",
                       source: str = "auto") -> str:
    """Execute a punishment action by name (``delete``/``warn``/``mute``/...)."""
    from .permissions import Actor as ActorType  # local import to avoid cycles

    actor = actor or ActorType(user_id=bot.id, chat_id=chat_id)
    action = (action or "none").strip().lower()
    delete_first = action.startswith("delete_")

    if delete_first and message_id:
        await safe_delete(bot, chat_id, message_id, context="auto_delete")
        action = action.replace("delete_", "", 1) or "delete"

    if action == "none":
        return ""
    if action == "delete":
        await safe_delete(bot, chat_id, message_id, context="auto_delete")
        await record_action(session, chat_id=chat_id, action="delete", target_id=target_id,
                            actor_id=actor.user_id, reason=reason, source=source)
        return "🗑 پیام حذف شد."
    if action == "warn":
        text, _ = await warn_user(bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
                                  target_name=target_name, reason=reason, chat_title=chat_title,
                                  source=source)
        return text
    if action in {"mute", "temp_mute"}:
        return await mute_user(bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
                               target_name=target_name,
                               duration=duration if action == "temp_mute" else None,
                               reason=reason, chat_title=chat_title, source=source)
    if action == "kick":
        return await kick_user(bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
                               target_name=target_name, reason=reason, chat_title=chat_title,
                               source=source)
    if action in {"ban", "temp_ban"}:
        return await ban_user(bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
                              target_name=target_name,
                              duration=duration if action == "temp_ban" else None,
                              reason=reason, chat_title=chat_title, source=source)
    return ""


async def user_history(session: AsyncSession, chat_id: int, user_id: int,
                       limit: int | None = None, offset: int = 0) -> list[ModerationAction]:
    """Return moderation events done to *or by* ``user_id`` in this chat.

    No default limit is applied: the history command is intended to be complete.
    Callers may request a page with ``limit`` and ``offset``.
    """
    statement = (
        select(ModerationAction)
        .where(
            ModerationAction.chat_id == chat_id,
            or_(ModerationAction.target_id == user_id,
                ModerationAction.actor_id == user_id),
        )
        .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
    )
    if limit is not None:
        statement = statement.limit(max(0, int(limit)))
    if offset:
        statement = statement.offset(max(0, int(offset)))
    result = await session.execute(statement)
    return list(result.scalars().all())


async def user_history_count(session: AsyncSession, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(func.count(ModerationAction.id)).where(
            ModerationAction.chat_id == chat_id,
            or_(ModerationAction.target_id == user_id,
                ModerationAction.actor_id == user_id),
        )
    )
    return int(result.scalar() or 0)


async def history_user_names(session: AsyncSession,
                             actions: list[ModerationAction]) -> dict[int, str]:
    """Resolve names for every actor and target shown in a history view."""
    user_ids = {
        user_id
        for item in actions
        for user_id in (item.actor_id, item.target_id, getattr(item, "undone_by", None))
        if user_id is not None
    }
    if not user_ids:
        return {}
    result = await session.execute(select(User).where(User.id.in_(user_ids)))
    names: dict[int, str] = {}
    for user in result.scalars().all():
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        names[user.id] = full_name or (f"@{user.username}" if user.username else str(user.id))
    for user_id in user_ids:
        names.setdefault(user_id, str(user_id))
    return names


async def clear_history(session: AsyncSession, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        delete(ModerationAction).where(
            ModerationAction.chat_id == chat_id,
            or_(ModerationAction.target_id == user_id,
                ModerationAction.actor_id == user_id),
        )
    )
    await session.execute(
        delete(ChatMemberState).where(ChatMemberState.chat_id == chat_id,
                                      ChatMemberState.user_id == user_id)
    )
    return int(result.rowcount or 0)


def _history_user(user_id: int | None, user_names: dict[int, str]) -> str:
    if user_id is None:
        return "سیستم"
    return escape(user_names.get(user_id, str(user_id)), quote=False)


def _history_source(source: str) -> str:
    return escape(HISTORY_SOURCE_LABELS_FA.get(source, "سیستم"), quote=False)


def history_text(actions: list[ModerationAction], title: str = "📜 تاریخچه کاربر", *,
                 user_id: int | None = None,
                 user_names: dict[int, str] | None = None,
                 total_count: int | None = None,
                 page: int = 0,
                 page_size: int | None = None) -> str:
    """Render a directional moderation-history page with second precision."""
    total = len(actions) if total_count is None else max(0, int(total_count))
    if not actions:
        return f"{escape(title, quote=False)}\n\nهیچ سابقه‌ای ثبت نشده است."

    names = user_names or {}
    header = f"🔢 تعداد رویدادها: {to_persian_digits(str(total))}"
    if page_size:
        pages = max(1, (total + page_size - 1) // page_size)
        header += (f"\n📄 صفحه {to_persian_digits(str(page + 1))} از "
                   f"{to_persian_digits(str(pages))}")
    blocks = [escape(title, quote=False), header]
    now = datetime.utcnow()
    reversible_actions = {"ban", "temp_ban", "mute", "temp_mute"}
    event_actions = {"unban", "unmute", "unwarn", "reset_warn"}

    for item in actions:
        label = escape(ACTION_LABELS_FA.get(item.action, item.action), quote=False)
        actor_name = _history_user(item.actor_id, names)
        target_name = _history_user(item.target_id, names)
        if user_id is not None and item.actor_id == user_id and item.target_id == user_id:
            direction = " (اقدام روی خودش)"
        elif user_id is not None and item.actor_id == user_id:
            direction = " (انجام‌شده توسط کاربر)"
        elif user_id is not None and item.target_id == user_id:
            direction = " (انجام‌شده روی کاربر)"
        else:
            direction = ""

        lines = [f"• {label}{direction}",
                 f"  👮 انجام‌دهنده: {actor_name}",
                 f"  🎯 هدف: {target_name}",
                 f"  📅 {persian_datetime(item.created_at, with_seconds=True)}"]
        if item.duration:
            lines.append(f"  ⏱ مدت: {escape(format_duration(item.duration), quote=False)}")
        if item.reason:
            lines.append(f"  📌 دلیل: {escape(item.reason, quote=False)}")

        if item.action in reversible_actions:
            if item.active and item.expires_at is not None and item.expires_at <= now:
                lines.append("  🔄 وضعیت: منقضی‌شده")
            elif item.active:
                lines.append("  🔄 وضعیت: فعال")
            elif item.undone_at is not None:
                lines.append(
                    f"  🔄 وضعیت: لغوشده در "
                    f"{persian_datetime(item.undone_at, with_seconds=True)} "
                    f"توسط {_history_user(item.undone_by, names)}"
                )
            else:
                lines.append("  🔄 وضعیت: پایان‌یافته")
        elif item.action in event_actions:
            # These records describe the reversal/reset itself, not a live state.
            if item.source and item.source not in {"command", "panel"}:
                lines.append(f"  ⚙️ منبع: {_history_source(item.source)}")
        elif item.source and item.source not in {"command", "panel"}:
            lines.append(f"  ⚙️ منبع: {_history_source(item.source)}")

        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def split_history_text(text: str, max_chars: int = 3800) -> list[str]:
    """Split a rendered history into Telegram-safe messages at event boundaries."""
    max_chars = max(500, min(int(max_chars), 4000))
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]
