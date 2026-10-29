"""Private, chat-scoped reports for every successful group ban.

Bot API restrictions mean a bot cannot start a private conversation with a user.
If a recipient has not opened the bot, the report is kept in an outbox and
retried the next time that recipient sends /start.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_settings
from ..core.errors import safe_send
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_datetime
from ..db.models import BotRootGrant, Chat, PendingPrivateReport, User

logger = logging.getLogger("armando.ban_reports")

SOURCE_LABELS_FA = {
    "command": "فرمان مدیر",
    "panel": "پنل مدیریت",
    "multi_ban": "مولتی‌بن",
    "antiflood": "ضدفلاود",
    "antispam": "ضداسپم",
    "filter": "فیلتر گروه",
    "lock": "قفل محتوا",
    "ai": "نظارت خودکار",
    "nightmode": "حالت شب",
    "captcha": "کپچا",
    "warn_threshold": "رسیدن به سقف اخطار",
    "leave_guard": "قفل خروج",
    "global_ban": "بن سراسری",
    "federation": "بن فدراسیون",
    "telegram_manual": "اقدام مدیر در تلگرام",
    "sticky_reban": "اجرای دوبارهٔ بن فعال",
    "deleted_account": "حساب حذف‌شده",
}


def _user_name(user, fallback: str = "") -> str:
    if user is None:
        return fallback or "کاربر"
    full = (f"{getattr(user, 'first_name', '') or ''} "
            f"{getattr(user, 'last_name', '') or ''}").strip()
    return full or getattr(user, "username", None) or fallback or "کاربر"


def _mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{html.escape(name)}</a>'


async def _recipient_ids(session: AsyncSession, chat_id: int) -> set[int]:
    recipients: set[int] = set()
    if app_settings.owner_id:
        recipients.add(int(app_settings.owner_id))
    try:
        rows = await session.execute(
            select(BotRootGrant.user_id, BotRootGrant.permissions)
            .where(BotRootGrant.chat_id == int(chat_id)))
        for user_id, permissions in rows.all():
            if permissions:
                recipients.add(int(user_id))
    except Exception as exc:  # noqa: BLE001 - reporting must not interrupt moderation
        logger.warning("root recipients lookup failed chat=%s error=%s",
                       chat_id, exc.__class__.__name__)
    return recipients


async def _report_text(session: AsyncSession, *, chat_id: int, target_id: int,
                       actor_id: int | None, bot_id: int | None, reason: str,
                       duration: int | None, source: str, chat_title: str = "",
                       target_name: str = "", target_username: str = "",
                       actor_name: str = "", actor_username: str = "",
                       chat=None, target=None, actor=None, action: str = "ban") -> str:
    chat_row = chat if chat is not None else await session.get(Chat, int(chat_id))
    target_row = target if target is not None else await session.get(User, int(target_id))
    actor_row = actor if actor is not None else (
        await session.get(User, int(actor_id)) if actor_id is not None else None)

    title = (getattr(chat_row, "title", None) or getattr(chat, "title", None)
             or chat_title or f"گروه {chat_id}")
    group_username = getattr(chat_row, "username", None) or getattr(chat, "username", None)
    group_name = html.escape(str(title))
    group_line = (f'<a href="https://t.me/{html.escape(str(group_username), quote=True)}">'
                  f"{group_name}</a>" if group_username else f"<b>{group_name}</b>")

    target_name = _user_name(target_row or target, target_name or f"کاربر {target_id}")
    target_line = _mention(target_id, target_name)
    target_username = (getattr(target_row or target, "username", None)
                       or target_username or "").strip()
    target_username = ("@" + target_username.lstrip("@")) if target_username else "—"

    if actor_id is not None and bot_id is not None and int(actor_id) == int(bot_id):
        actor_name = "🤖 آرماندو (ربات)"
        actor_line = f"{actor_name} — <code>{int(actor_id)}</code>"
    elif actor_id is None:
        actor_line = "🤖 سامانهٔ خودکار"
    else:
        actor_name = _user_name(actor_row or actor, actor_name or f"کاربر {actor_id}")
        actor_username = (getattr(actor_row or actor, "username", None)
                          or actor_username or "").strip()
        suffix = f" — @{html.escape(actor_username.lstrip('@'))}" if actor_username else ""
        actor_line = f"{_mention(int(actor_id), actor_name)}{suffix} — <code>{int(actor_id)}</code>"

    action_label = {
        "ban": "بن", "temp_ban": "بن موقت",
        "kick": "اخراج همراه با بن فعال",
    }.get(action, action)
    if duration:
        try:
            from .moderation import format_duration

            duration_text = format_duration(int(duration))
        except Exception:  # noqa: BLE001
            duration_text = f"{to_persian_digits(str(int(duration)))} ثانیه"
    else:
        duration_text = "دائمی"

    reason_text = html.escape((reason or "از تلگرام گزارش نشده").strip()[:700])
    source_text = html.escape(SOURCE_LABELS_FA.get(source, source or "نامشخص"))
    return "\n".join([
        "🛡 <b>گزارش کامل بن</b>",
        "",
        f"💬 گروه: {group_line}",
        f"🆔 شناسهٔ گروه: <code>{int(chat_id)}</code>",
        f"👤 هدف: {target_line}",
        f"🔗 نام کاربری هدف: {html.escape(target_username)}",
        f"🆔 شناسهٔ هدف: <code>{int(target_id)}</code>",
        f"👮 انجام‌دهنده: {actor_line}",
        f"⚖️ نوع اقدام: {html.escape(action_label)}",
        f"📌 دلیل: {reason_text}",
        f"⏱ مدت: {html.escape(duration_text)}",
        f"🧭 منبع: {source_text}",
        f"🕒 زمان: {html.escape(persian_datetime())}",
    ])


async def report_successful_ban(bot: Bot, session: AsyncSession, *, chat_id: int,
                               target_id: int, actor_id: int | None,
                               reason: str = "", duration: int | None = None,
                               source: str = "command", chat_title: str = "",
                               target_name: str = "", target_username: str = "",
                               actor_name: str = "", actor_username: str = "",
                               chat=None, target=None, actor=None,
                               action: str = "ban") -> int:
    """Send a detailed private report to the owner and scoped roots.

    Failed private deliveries are queued. Telegram requires a user to start the
    bot before the bot can message them; queued items are retried on /start.
    """
    recipients = await _recipient_ids(session, chat_id)
    if not recipients:
        logger.warning("ban report has no recipients chat=%s target=%s", chat_id, target_id)
        return 0

    text = await _report_text(
        session, chat_id=chat_id, target_id=target_id, actor_id=actor_id,
        bot_id=getattr(bot, "id", None), reason=reason, duration=duration,
        source=source, chat_title=chat_title, target_name=target_name,
        target_username=target_username, actor_name=actor_name,
        actor_username=actor_username, chat=chat, target=target,
        actor=actor, action=action)
    delivered = 0
    queued = 0
    for recipient_id in sorted(recipients):
        sent = await safe_send(bot, recipient_id, text, parse_mode="HTML",
                               disable_web_page_preview=True)
        if sent is not None:
            delivered += 1
            continue
        session.add(PendingPrivateReport(
            recipient_id=recipient_id, chat_id=int(chat_id), target_id=int(target_id),
            report_text=text, created_at=datetime.utcnow()))
        queued += 1
    if queued:
        await session.flush()
    logger.info("ban report chat=%s target=%s recipients=%s delivered=%s queued=%s source=%s",
                chat_id, target_id, len(recipients), delivered, queued, source)
    return delivered


async def deliver_pending_reports(bot: Bot, session: AsyncSession, recipient_id: int,
                                 *, limit: int = 20) -> int:
    """Retry all queued reports after /start; retain any item Telegram rejects."""
    page_size = max(1, min(int(limit), 50))
    delivered = 0
    while True:
        result = await session.execute(
            select(PendingPrivateReport)
            .where(PendingPrivateReport.recipient_id == int(recipient_id))
            .order_by(PendingPrivateReport.created_at, PendingPrivateReport.id)
            .limit(page_size))
        rows = list(result.scalars().all())
        if not rows:
            break
        for row in rows:
            sent = await safe_send(bot, int(recipient_id), row.report_text,
                                   parse_mode="HTML", disable_web_page_preview=True)
            if sent is None:
                if delivered:
                    await session.flush()
                logger.info("pending ban report retained recipient=%s id=%s",
                            recipient_id, row.id)
                return delivered
            await session.delete(row)
            delivered += 1
        await session.flush()
        if len(rows) < page_size:
            break
    if delivered:
        logger.info("delivered %s queued ban reports recipient=%s", delivered, recipient_id)
    return delivered
