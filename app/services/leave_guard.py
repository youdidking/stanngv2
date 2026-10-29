"""«قفل خروج» - automatically ban members who leave the group.

Two independent rules, both off by default (a public bot must never surprise a
group with bans nobody asked for):

``ban_on_leave``
    every member who leaves is banned right away,
``quick_leave_ban`` + ``quick_leave_seconds``
    only members who leave again within a few seconds of joining are banned
    (the classic "join & leave" spam pattern).

Both rules ignore administrators and the bot itself, and both need the bot to
hold the «محدود کردن اعضا» right.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select

from ..config import settings as app_settings
from ..core.errors import safe_ban, safe_send
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_datetime
from ..db.models import BotRootGrant
from .permissions import bot_has_right, fetch_chat_member

logger = logging.getLogger("armando.leave_guard")


def is_enabled(settings: dict) -> bool:
    return bool(settings.get("ban_on_leave")) or bool(settings.get("quick_leave_ban"))


def status_text(settings: dict) -> str:
    """A one-line Persian summary of leave bans and private reporting."""
    parts = []
    if settings.get("ban_on_leave"):
        parts.append("هر خروج → بن")
    if settings.get("quick_leave_ban"):
        seconds = int(settings.get("quick_leave_seconds") or 10)
        parts.append(f"خروج تا {to_persian_digits(str(seconds))} ثانیه پس از ورود → بن")
    if settings.get("leave_notify_owner"):
        parts.append("گزارش خصوصی خروج‌های بدون بن روشن")
    if not parts:
        return "🔓 قفل خروج و گزارش خصوصی خاموش‌اند"
    return "🚪 " + " • ".join(parts)


async def notify_leave_event(bot: Bot, session, *, chat, user, settings: dict,
                             ban_reason: str | None) -> int:
    """Privately report a departure to the bot owner and this chat's root delegates.

    Returns the number of recipients who could be reached. This lets the caller
    keep the group notice if private delivery failed for everyone.
    """
    if not settings.get("leave_notify_owner"):
        return 0
    recipients: set[int] = set()
    if app_settings.owner_id:
        recipients.add(int(app_settings.owner_id))
    try:
        result = await session.execute(
            select(BotRootGrant.user_id, BotRootGrant.permissions)
            .where(BotRootGrant.chat_id == int(chat.id)))
        for user_id, permissions in result.all():
            if permissions:
                recipients.add(int(user_id))
    except Exception as exc:  # noqa: BLE001 - reporting must not interrupt moderation
        logger.warning("could not resolve leave-report recipients (%s)", exc.__class__.__name__)

    if ban_reason:
        outcome = f"✅ بن خودکار انجام شد؛ دلیل: {html.escape(str(ban_reason))}"
    elif settings.get("ban_on_leave"):
        outcome = "⚠️ بن خودکار انجام نشد؛ دسترسی بن بات و گزارش تلگرام را بررسی کنید."
    elif settings.get("quick_leave_ban"):
        outcome = "ℹ️ خروج ثبت شد؛ شرط خروج سریع برقرار نبود یا بن انجام نشد."
    else:
        outcome = "ℹ️ بن خودکار برای این گروه خاموش است."

    title = html.escape(str(getattr(chat, "title", "") or "گروه بدون نام"))
    username = getattr(chat, "username", None)
    if username:
        group_line = f"گروه: <a href=\"https://t.me/{html.escape(str(username), quote=True)}\">{title}</a>"
    else:
        group_line = f"گروه: <b>{title}</b>"
    full_name = html.escape(str(getattr(user, "full_name", "") or "کاربر"))
    username = getattr(user, "username", None)
    user_ref = (f"<a href=\"tg://user?id={int(user.id)}\">{full_name}</a>"
                if getattr(user, "id", None) else full_name)
    message = (
        "📬 <b>گزارش خروج از گروه</b>\n\n"
        f"{group_line}\n"
        f"شناسهٔ گروه: <code>{int(chat.id)}</code>\n"
        f"کاربر: {user_ref}\n"
        f"نام کاربری: {('@' + html.escape(str(username))) if username else '—'}\n"
        f"شناسهٔ کاربر: <code>{int(user.id)}</code>\n"
        f"وضعیت: {outcome}\n"
        f"زمان: {html.escape(persian_datetime())}"
    )
    delivered = 0
    for recipient_id in sorted(recipients):
        sent = await safe_send(bot, recipient_id, message, parse_mode="HTML",
                               disable_web_page_preview=True)
        if sent is not None:
            delivered += 1
    logger.info("leave report chat=%s user=%s recipients=%s delivered=%s banned=%s",
                chat.id, user.id, len(recipients), delivered, bool(ban_reason))
    return delivered


async def check_leave(bot: Bot, chat_id: int, user_id: int, settings: dict,
                      joined_at: datetime | None = None,
                      session=None, member_status: str | None = None,
                      chat_title: str = "") -> str | None:
    """Ban the member if a leave rule fires; return the Persian reason or None.

    ``session`` lets the guard write a ``ban_records`` row.  Without it the bot
    would ban the member on Telegram's side while having no idea they are
    banned - so the sticky-ban re-entry check could never fire for them.
    """
    if not is_enabled(settings):
        return None
    if not await bot_has_right(bot, chat_id, "can_restrict_members"):
        logger.debug("leave guard skipped: bot may not restrict members in %s", chat_id)
        return None

    status = member_status
    if status is None:
        member = await fetch_chat_member(bot, chat_id, user_id, refresh=True)
        status = getattr(member, "status", "")
    if status in {"administrator", "creator", "kicked"}:
        return None  # never touch admins, and a banned user is already gone

    reason: str | None = None
    if settings.get("ban_on_leave"):
        reason = "خروج از گروه (قفل خروج)"
    elif settings.get("quick_leave_ban"):
        seconds = int(settings.get("quick_leave_seconds") or 10)
        if joined_at is not None:
            elapsed = (datetime.utcnow() - joined_at).total_seconds()
            if 0 <= elapsed <= seconds:
                reason = (f"خروج سریع: {to_persian_digits(str(int(elapsed)))} ثانیه "
                          f"پس از ورود")
        else:
            reason = "خروج بلافاصله پس از ورود"

    if reason is None:
        return None

    # Explicit zero means a permanent Telegram ban. In supergroups this blocks
    # the same account from rejoining through invite links until an admin
    # explicitly unbans it.
    banned = await safe_ban(bot, chat_id, user_id, until_date=0,
                            context="leave_guard_ban")
    if not banned:
        return None

    if session is not None:
        try:
            from ..db.models import BanRecord

            session.add(BanRecord(
                chat_id=chat_id, user_id=user_id, banned_by=None,
                reason=(reason or "")[:500], scope="chat", active=True,
                until=None, created_at=datetime.utcnow()))
            await session.flush()
        except Exception as exc:  # noqa: BLE001 - never lose the ban itself
            logger.warning("could not record the leave-guard ban: %s", exc)

    try:
        from .ban_reports import report_successful_ban

        await report_successful_ban(
            bot, session, chat_id=chat_id, target_id=user_id, actor_id=bot.id,
            reason=reason, source="leave_guard", target_name=str(user_id),
            chat_title=chat_title)
    except Exception as exc:  # noqa: BLE001 - a report must never undo a successful ban
        logger.warning("leave-ban private report failed (%s)", exc.__class__.__name__)
    logger.info("کاربر %s در گروه %s به‌دلیل خروج بن شد (%s)", user_id, chat_id, reason)
    return reason
