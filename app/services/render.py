"""Placeholder rendering and media/button response delivery.

Placeholders supported in any user-defined text:
``{first_name} {last_name} {full_name} {username} {user} {user_id} {chat_id}
{chat_name} {group_name} {bot_username} {date} {time} {year} {month} {day}
{role} {mention} {members} {tag}``
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_date, persian_time
from ..keyboards.factory import build_markup
from .roles import role_name_fa

logger = logging.getLogger("armando.render")

PLACEHOLDER_RE = re.compile(r"\{(\w+)}")


async def build_context(session: AsyncSession, bot: Bot, chat_id: int, user_id: int | None = None,
                        *, chat_title: str = "", user=None, chat=None,
                        member_count: int | None = None) -> dict[str, Any]:
    from ..db.models import Chat, ChatMemberState, User

    def safe(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)

    values: dict[str, Any] = {
        "chat_id": str(chat_id),
        "chat_name": safe(chat_title),
        "group_name": safe(chat_title),
        "bot_username": "",
        "members": to_persian_digits(str(member_count or 0)),
        "date": safe(persian_date()),
        "time": safe(persian_time()),
        "year": "", "month": "", "day": "",
        "first_name": "", "last_name": "", "full_name": "",
        "username": "ندارد", "user_id": str(user_id or ""),
        "user": "", "mention": "", "role": "", "tag": "",
    }
    try:
        bot_user = await safe_call(lambda: bot.get_me(), default=None, log=False)
        if bot_user is not None:
            values["bot_username"] = safe(bot_user.username)
    except Exception:  # noqa: BLE001
        pass
    from ..core.timeutils import jalali
    jd = jalali()
    values["year"] = to_persian_digits(str(jd.year))
    values["month"] = to_persian_digits(str(jd.month))
    values["day"] = to_persian_digits(str(jd.day))

    if chat is None:
        chat = await session.get(Chat, chat_id)
    if chat is not None:
        title = safe(getattr(chat, "title", "") or chat_title)
        values["chat_name"] = title
        values["group_name"] = title
        values["members"] = to_persian_digits(str(
            member_count if member_count is not None else getattr(chat, "member_count", 0) or 0))

    if user_id:
        db_user = await session.get(User, user_id)
        source_user = db_user or user
        if source_user is not None:
            first_raw = getattr(source_user, "first_name", "") or ""
            last_raw = getattr(source_user, "last_name", "") or ""
            username_raw = getattr(source_user, "username", None)
            full_raw = f"{first_raw} {last_raw}".strip() or str(user_id)
            first = safe(first_raw)
            values.update({
                "first_name": first,
                "last_name": safe(last_raw),
                "full_name": safe(full_raw),
                "username": safe("@" + str(username_raw).lstrip("@")) if username_raw else "ندارد",
                "user": safe(full_raw),
                "mention": f'<a href="tg://user?id={int(user_id)}">{first or str(user_id)}</a>',
            })
        else:
            values.update({
                "first_name": "کاربر", "full_name": f"کاربر {user_id}",
                "user": f"کاربر {user_id}", "user_id": str(user_id),
                "mention": f'<a href="tg://user?id={int(user_id)}">کاربر {user_id}</a>',
            })
        result = await session.execute(
            select(ChatMemberState).where(
                ChatMemberState.chat_id == chat_id, ChatMemberState.user_id == user_id)
        )
        state = result.scalar_one_or_none()
        values["role"] = safe(role_name_fa(state.bot_role if state else None))
        values["tag"] = safe(state.tag if state and state.tag else "")
    return values


def fill_placeholders(text: str, context: dict[str, Any]) -> str:
    if not text:
        return ""
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return str(context.get(key, match.group(0)))
    return PLACEHOLDER_RE.sub(_replace, text)


async def send_content(bot: Bot, chat_id: int, *, content_type: str = "text", text: str = "",
                       file_id: str | None = None, caption: str | None = None,
                       buttons: Any = None, reply_to_message_id: int | None = None,
                       context: dict[str, Any] | None = None, parse_mode: str = ParseMode.HTML,
                       disable_preview: bool = True, delete_after: int = 0):
    """Send any stored content type and return the sent message (or None)."""
    ctx = context or {}
    markup = build_markup(buttons)
    body = fill_placeholders(text or "", ctx)
    cap = fill_placeholders(caption or "", ctx) if caption else None

    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "reply_markup": markup,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_to_message_id:
        kwargs["reply_to_message_id"] = reply_to_message_id

    content_type = (content_type or "text").lower()
    try:
        if content_type == "text" or not file_id:
            if not body and not cap:
                return None
            result = await safe_call(
                lambda: bot.send_message(text=body or (cap or ""), **kwargs), context="send_content_text")
        elif content_type == "photo":
            result = await safe_call(lambda: bot.send_photo(photo=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_photo")
        elif content_type == "video":
            result = await safe_call(lambda: bot.send_video(video=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_video")
        elif content_type == "animation":
            result = await safe_call(lambda: bot.send_animation(animation=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_animation")
        elif content_type == "audio":
            result = await safe_call(lambda: bot.send_audio(audio=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_audio")
        elif content_type == "voice":
            result = await safe_call(lambda: bot.send_voice(voice=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_voice")
        elif content_type == "document":
            result = await safe_call(lambda: bot.send_document(document=file_id, caption=cap or body or None, **kwargs),
                                     context="send_content_document")
        elif content_type == "sticker":
            result = await safe_call(lambda: bot.send_sticker(sticker=file_id,
                                                              reply_markup=markup,
                                                              reply_to_message_id=reply_to_message_id,
                                                              chat_id=chat_id), context="send_content_sticker")
        elif content_type == "video_note":
            result = await safe_call(lambda: bot.send_video_note(video_note=file_id, chat_id=chat_id,
                                                                 reply_markup=markup,
                                                                 reply_to_message_id=reply_to_message_id),
                                     context="send_content_video_note")
        else:
            result = await safe_call(lambda: bot.send_message(text=body or (cap or ""), **kwargs),
                                     context="send_content_fallback")
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_content failed: %s", exc)
        return None
    return result


CONTENT_TYPES = {
    "text": "📝 متن",
    "photo": "🖼 عکس",
    "video": "🎞 ویدیو",
    "animation": "🌀 گیف",
    "audio": "🎵 آهنگ",
    "voice": "🎙 ویس",
    "document": "📁 فایل",
    "sticker": "🎴 استیکر",
    "video_note": "🎥 ویدیو پیام",
}
