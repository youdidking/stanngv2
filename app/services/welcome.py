"""Welcome / goodbye system and service-message cleaner."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_delete
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_date
from ..db.models import ChatMemberState
from .chat_state import get_settings, invalidate_member
from .render import build_context, send_content

logger = logging.getLogger("armando.welcome")

DEFAULT_WELCOME = (
    "╭─── ༺ خوش‌آمدید ༻ ───╮\n"
    "🌷 سلام {mention} عزیز!\n\n"
    "🆔 شناسهٔ شما: <code>{user_id}</code>\n"
    "🔗 نام کاربری: <code>{username}</code>\n"
    "💬 گروه: <b>{chat_name}</b>\n"
    "🗓 زمان ورود: {date}، ساعت {time}\n\n"
    "📜 لطفاً پیش از گفتگو، قوانین گروه را مطالعه کنید.\n"
    "╰────────────────────╯"
)
DEFAULT_GOODBYE = "👋 {first_name} از گروه رفت. به امید دیدار دوباره."


async def send_welcome(bot: Bot, session: AsyncSession, *, chat_id: int, user,
                       chat_title: str = "", member_count: int | None = None) -> Message | None:
    settings_obj = await get_settings(session, chat_id)
    if not settings_obj.welcome_enabled:
        return None
    context = await build_context(session, bot, chat_id, user.id if user else None,
                                  chat_title=chat_title, user=user, member_count=member_count)
    media = settings_obj.welcome_media or {}
    content_type = media.get("type", "text") if media else "text"
    text = settings_obj.welcome_text or DEFAULT_WELCOME
    message = await send_content(
        bot, chat_id,
        content_type=content_type,
        text=text,
        file_id=media.get("file_id"),
        caption=media.get("caption") or (text if content_type != "text" else None),
        buttons=settings_obj.welcome_buttons,
        context=context,
    )
    # Outgoing group messages receive the application-wide 30-second TTL in
    # the Bot API request middleware; no second per-welcome timer is needed.
    return message


async def send_goodbye(bot: Bot, session: AsyncSession, *, chat_id: int, user,
                       chat_title: str = "") -> Message | None:
    settings_obj = await get_settings(session, chat_id)
    if not settings_obj.goodbye_enabled:
        return None
    context = await build_context(session, bot, chat_id, user.id if user else None,
                                  chat_title=chat_title, user=user)
    media = settings_obj.goodbye_media or {}
    content_type = media.get("type", "text") if media else "text"
    text = settings_obj.goodbye_text or DEFAULT_GOODBYE
    message = await send_content(
        bot, chat_id,
        content_type=content_type,
        text=text,
        file_id=media.get("file_id"),
        caption=media.get("caption") or (text if content_type != "text" else None),
        buttons=settings_obj.goodbye_buttons,
        context=context,
    )
    # The global group TTL also covers goodbye messages.
    return message


SERVICE_SWITCHES = {
    "new_chat_members": "clean_join",
    "left_chat_member": "clean_leave",
    "pinned_message": "clean_pin",
    "new_chat_title": "clean_service",
    "new_chat_photo": "clean_service",
    "delete_chat_photo": "clean_service",
    "group_chat_created": "clean_service",
    "voice_chat_started": "clean_voice_chat",
    "voice_chat_ended": "clean_voice_chat",
    "message_auto_delete_timer_changed": "clean_service",
}


async def maybe_clean_service(bot: Bot, session: AsyncSession, message: Message,
                              *, chat_id: int, chat_type: str = "group") -> bool:
    """Delete a service message when the matching switch is enabled."""
    if chat_type not in {"group", "supergroup"}:
        return False
    settings_obj = await get_settings(session, chat_id)
    for field_name, switch in SERVICE_SWITCHES.items():
        if getattr(message, field_name, None) is not None:
            if bool(getattr(settings_obj, switch, False)):
                await safe_delete(bot, chat_id, message.message_id, context="clean_service")
                return True
    # Linked channel posts
    if getattr(message, "sender_chat", None) is not None and getattr(message, "is_automatic_forward", False):
        if bool(settings_obj.clean_channel_post):
            await safe_delete(bot, chat_id, message.message_id, context="clean_channel_post")
            return True
    return False


async def clean_bot_join_messages(bot: Bot, session: AsyncSession, chat_id: int,
                                  chat_member) -> None:
    """Used when the bot itself is added to a group."""
    settings_obj = await get_settings(session, chat_id)
    if bool(settings_obj.clean_join):
        await safe_delete(bot, chat_id, chat_member.message_id if hasattr(chat_member, "message_id") else 0,
                          context="clean_bot_join")


async def record_join(session: AsyncSession, chat_id: int, user) -> ChatMemberState:
    state = None
    from .chat_state import get_member_state

    state = await get_member_state(session, chat_id, user.id)
    state.joined_at = datetime.utcnow()
    state.left_at = None
    state.status = "member"
    state.updated_at = datetime.utcnow()
    await session.flush()
    invalidate_member(chat_id, user.id)
    return state


async def record_leave(session: AsyncSession, chat_id: int, user_id: int) -> None:
    from .chat_state import get_member_state

    state = await get_member_state(session, chat_id, user_id)
    state.left_at = datetime.utcnow()
    state.status = "left"
    state.updated_at = datetime.utcnow()
    await session.flush()
    invalidate_member(chat_id, user_id)


def stats_line(member_count: int) -> str:
    return f"👥 تعداد اعضا: {to_persian_digits(str(member_count))} | 📅 {persian_date()}"
