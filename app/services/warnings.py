"""Warning threshold handling and the Persian warning status card."""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.duration import format_duration
from ..core.timeutils import persian_datetime
from .chat_state import get_settings
from .permissions import Actor

logger = logging.getLogger("armando.warnings")

THRESHOLD_ACTIONS_FA = {
    "none": "بدون اقدام",
    "warn": "فقط اخطار",
    "mute": "سکوت",
    "temp_mute": "سکوت موقت",
    "kick": "اخراج",
    "ban": "بن",
    "temp_ban": "بن موقت",
}


async def threshold_action(bot: Bot, session: AsyncSession, *, chat_id: int, actor: Actor,
                           target_id: int, target_name: str, action: str,
                           duration: int | None, reason: str, chat_title: str = "") -> tuple[str, str]:
    """Execute the configured action when the warning limit is reached."""
    from . import moderation

    action = (action or "none").lower()
    if action == "none":
        return (f"⚠️ کاربر به سقف اخطار رسید اما هیچ اقدامی تنظیم نشده است.\n📌 {reason}", "warn")
    if action == "warn":
        return (f"⚠️ کاربر به سقف اخطار رسید.\n📌 {reason}", "warn")
    if action in {"mute", "temp_mute"}:
        await moderation.mute_user(
            bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
            target_name=target_name, duration=duration if action == "temp_mute" else None,
            reason=reason, chat_title=chat_title, source="warn_threshold")
        return (f"🚫 کاربر به سقف اخطار رسید و {'موقتاً ' if action == 'temp_mute' else ''}بی‌صدا شد."
                + (f"\n⏱ مدت: {format_duration(duration)}" if action == "temp_mute" else "")
                + f"\n📌 {reason}", action)
    if action == "kick":
        await moderation.kick_user(
            bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
            target_name=target_name, reason=reason, chat_title=chat_title,
            source="warn_threshold")
        return (f"🚫 کاربر به سقف اخطار رسید و اخراج شد.\n📌 {reason}", "kick")
    if action in {"ban", "temp_ban"}:
        await moderation.ban_user(
            bot, session, chat_id=chat_id, actor=actor, target_id=target_id,
            target_name=target_name, duration=duration if action == "temp_ban" else None,
            reason=reason, chat_title=chat_title, source="warn_threshold")
        return (f"🚫 کاربر به سقف اخطار رسید و {'موقتاً ' if action == 'temp_ban' else ''}بن شد."
                + (f"\n⏱ مدت: {format_duration(duration)}" if action == "temp_ban" else "")
                + f"\n📌 {reason}", action)
    return (f"⚠️ سقف اخطار رسیده است.\n📌 {reason}", "warn")


def warn_card(name: str, count: int, limit: int, last_reason: str = "",
              last_time: str = "", user_id: int | None = None) -> str:
    from ..core.normalization import to_persian_digits

    filled = min(count, limit)
    bar = "🟩" * filled + "⬜️" * max(0, limit - filled)
    lines = [
        "⚠️ <b>وضعیت اخطار کاربر</b>",
        "",
        f"👤 کاربر: {name}",
        f"🔢 تعداد اخطار: {to_persian_digits(str(count))} از {to_persian_digits(str(limit))}",
        bar,
    ]
    if last_reason:
        lines.append(f"📌 آخرین دلیل: {last_reason}")
    if last_time:
        lines.append(f"🕒 آخرین اخطار: {last_time}")
    return "\n".join(lines)


def warn_history_text(records, title: str = "📜 تاریخچه اخطارها") -> str:
    if not records:
        return f"{title}\n\nهیچ اخطاری ثبت نشده است."
    lines = [title, ""]
    for index, record in enumerate(records, start=1):
        status = "✅ فعال" if record.active else "🚫 حذف شده"
        lines.append(
            f"{index}. {persian_datetime(record.created_at)} | {status}\n"
            f"   📌 دلیل: {record.reason or '—'}\n"
            f"   👮 توسط: {record.moderator_id or '—'}"
        )
    return "\n".join(lines)


async def warn_limit_of(session: AsyncSession, chat_id: int) -> int:
    settings_obj = await get_settings(session, chat_id)
    return int(settings_obj.warn_limit or 4)
