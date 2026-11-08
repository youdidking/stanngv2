"""Audit log + optional Telegram log channel fan-out."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.errors import safe_call
from ..core.timeutils import persian_datetime
from ..db.models import AuditLog

logger = logging.getLogger("armando.audit")

ACTION_LABELS_FA: dict[str, str] = {
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
    "purge": "🧹 پاکسازی",
    "pin": "📌 پین",
    "unpin": "📍 حذف پین",
    "unpin_all": "🗑 حذف همه پین‌ها",
    "lock": "🔒 قفل",
    "unlock": "🔓 باز کردن قفل",
    "filter_add": "🚫 افزودن فیلتر",
    "filter_remove": "🚮 حذف فیلتر",
    "note_save": "📝 ذخیره یادداشت",
    "note_delete": "🗑 حذف یادداشت",
    "staff_add": "👮 افزودن مدیر",
    "staff_remove": "🚫 عزل مدیر",
    "trust_add": "⭐️ کاربر ویژه",
    "trust_remove": "🚫 عزل ویژه",
    "captcha_fail": "🤖 شکست در کپچا",
    "captcha_pass": "✅ عبور از کپچا",
    "raid": "🛡 حالت ضد Raid",
    "report": "📢 گزارش",
    "fed_ban": "🌐 بن فدراسیون",
    "fed_unban": "🌐 رفع بن فدراسیون",
    "global_ban": "🌍 بن سراسری",
    "global_unban": "🌍 رفع بن سراسری",
    "tag_set": "🏷 تغییر تگ",
    "settings": "⚙️ تغییر تنظیمات",
    "schedule": "⏰ زمان‌بندی",
    "night_mode": "🌙 حالت شب",
    "backup": "💾 پشتیبان‌گیری",
    "restore": "♻️ بازیابی",
}


def action_label(action: str) -> str:
    return ACTION_LABELS_FA.get(action, f"• {action}")


async def log_event(session: AsyncSession, *, chat_id: int, action: str, actor_id: int | None = None,
                    target_id: int | None = None, reason: str = "", payload: dict | None = None) -> AuditLog:
    entry = AuditLog(
        chat_id=chat_id,
        actor_id=actor_id,
        action=action,
        target_id=target_id,
        reason=(reason or "")[:1000],
        payload=payload or {},
        created_at=datetime.utcnow(),
    )
    session.add(entry)
    await session.flush()
    logger.info("audit chat=%s action=%s actor=%s target=%s", chat_id, action, actor_id, target_id)
    return entry


def mention_html(user_id: int, name: str) -> str:
    escaped = (name or "کاربر").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{escaped}</a>'


# Shared alias used across handlers and services.
html_user = mention_html


def build_log_text(*, action: str, actor: str, target: str, chat_title: str = "", reason: str = "",
                   duration: str = "", extra: str = "") -> str:
    lines = [f"{action_label(action)}", ""]
    if chat_title:
        lines.append(f"💬 گروه: {chat_title}")
    lines.append(f"👤 کاربر هدف: {target}")
    lines.append(f"👮 مدیر: {actor}")
    if duration:
        lines.append(f"⏱ مدت: {duration}")
    if reason:
        lines.append(f"📌 دلیل: {reason}")
    if extra:
        lines.append(extra)
    lines.append(f"🕒 زمان: {persian_datetime()}")
    return "\n".join(lines)


async def send_log(bot: Bot, chat_id: int, text: str, *, session: AsyncSession | None = None,
                   log_chat_id: int | None = None) -> None:
    """Send a formatted moderation log to the configured log channel."""
    target = log_chat_id
    if session is not None and target is None:
        from .chat_state import get_settings_cached

        data = await get_settings_cached(session, chat_id)
        target = data.get("log_chat_id")
    target = target if target is not None else settings.log_chat_id
    if not target:
        return
    await safe_call(lambda: bot.send_message(chat_id=int(target), text=text,
                                             parse_mode=ParseMode.HTML,
                                             disable_web_page_preview=True),
                    context="audit_log")


async def audit_and_log(session: AsyncSession, bot: Bot, *, chat_id: int, action: str,
                        actor_id: int | None, target_id: int | None, actor_name: str = "",
                        target_name: str = "", chat_title: str = "", reason: str = "",
                        duration: str = "", payload: dict[str, Any] | None = None,
                        extra: str = "") -> None:
    """Persist the audit entry and mirror it to the log channel."""
    await log_event(session, chat_id=chat_id, action=action, actor_id=actor_id,
                    target_id=target_id, reason=reason, payload=payload)
    text = build_log_text(
        action=action,
        actor=actor_name or (mention_html(actor_id, "سیستم") if actor_id else "سیستم"),
        target=target_name or (mention_html(target_id, "کاربر") if target_id else "—"),
        chat_title=chat_title,
        reason=reason,
        duration=duration,
        extra=extra,
    )
    await send_log(bot, chat_id, text, session=session)
