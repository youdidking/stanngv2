"""Editable per-chat member tags.

The tag is stored inside the bot **and** mirrored into the real Telegram
member list whenever the bot holds the needed administrator right:

* regular members -> ``setChatMemberTag`` (right: ``can_manage_tags``)
* administrators  -> ``setChatAdministratorCustomTitle`` (right:
  ``can_promote_members``, admin must have been promoted by the bot)
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import ChatMember, ChatMemberAdministrator, ChatMemberOwner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..core.normalization import to_persian_digits
from ..db.models import ChatMemberState
from .chat_state import get_member_state, invalidate_member

logger = logging.getLogger("armando.tags")

MAX_TAG_LENGTH = 32
# Telegram limits both the admin "custom title" and the member tag.
TELEGRAM_TITLE_LENGTH = 16
TELEGRAM_TAG_LENGTH = 16
FORBIDDEN_IN_TAG = ("@", "http", "t.me")


def clean_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    cleaned = " ".join((tag or "").split()).strip()
    if not cleaned:
        return None
    cleaned = cleaned[:MAX_TAG_LENGTH]
    return cleaned


async def set_tag(session: AsyncSession, chat_id: int, user_id: int, tag: str | None,
                  *, strict: bool = True) -> str | None:
    cleaned = clean_tag(tag)
    if cleaned and strict:
        lowered = cleaned.lower()
        for forbidden in FORBIDDEN_IN_TAG:
            if forbidden in lowered:
                raise ValueError("⛔️ تگ نمی‌تواند شامل لینک یا نام کاربری باشد.")
    state = await get_member_state(session, chat_id, user_id)
    state.tag = cleaned
    await session.flush()
    invalidate_member(chat_id, user_id)
    return cleaned


async def get_tag(session: AsyncSession, chat_id: int, user_id: int) -> str | None:
    result = await session.execute(
        select(ChatMemberState.tag).where(ChatMemberState.chat_id == chat_id,
                                          ChatMemberState.user_id == user_id)
    )
    return result.scalar_one_or_none()


def display_name(name: str, tag: str | None) -> str:
    if not tag:
        return name
    return f"{name} <code>[{tag}]</code>"


async def tags_of_chat(session: AsyncSession, chat_id: int, limit: int = 100) -> list[tuple[int, str]]:
    result = await session.execute(
        select(ChatMemberState.user_id, ChatMemberState.tag).where(
            ChatMemberState.chat_id == chat_id, ChatMemberState.tag.is_not(None))
        .limit(limit)
    )
    return [(int(row[0]), str(row[1])) for row in result.all()]


def tags_text(rows: list[tuple[int, str, str]], title: str = "🏷 تگ اعضا") -> str:
    if not rows:
        return f"{title}\n\nهیچ تگی ثبت نشده است."
    lines = [title, ""]
    for index, (user_id, name, tag) in enumerate(rows, start=1):
        lines.append(f"{to_persian_digits(str(index))}. {name} — <code>{tag}</code> — <code>{user_id}</code>")
    return "\n".join(lines)


def telegram_title(tag: str | None) -> str:
    """Shorten the tag to what Telegram accepts as an administrator title."""
    return (clean_tag(tag) or "")[:TELEGRAM_TITLE_LENGTH]


def telegram_tag(tag: str | None) -> str:
    """Shorten the tag to what Telegram accepts as a member tag."""
    return (clean_tag(tag) or "")[:TELEGRAM_TAG_LENGTH]


async def _set_admin_title(bot: Bot, chat_id: int, user_id: int, title: str) -> Any | None:
    """Set (or, with an empty string, clear) an administrator custom title."""
    from ..core.telegram_extra import call_api_raw

    return await call_api_raw(bot, "setChatAdministratorCustomTitle",
                              {"chat_id": chat_id, "user_id": user_id,
                               "custom_title": title})


async def _set_member_tag(bot: Bot, chat_id: int, user_id: int, tag: str) -> Any | None:
    """Set (or, with an empty string, clear) a regular member's tag."""
    from ..core.telegram_extra import call_api_raw

    return await call_api_raw(bot, "setChatMemberTag",
                              {"chat_id": chat_id, "user_id": user_id, "tag": tag})


async def apply_member_tag(bot: Bot, chat_id: int, user_id: int,
                           tag: str | None) -> tuple[bool, str]:
    """Mirror the tag into the Telegram member list.

    Returns ``(ok, reason)``; ``reason`` is a ready-to-send Persian sentence and
    is empty on success.
    """
    from .permissions import bot_has_right, fetch_chat_member

    try:
        member: ChatMember | None = await fetch_chat_member(bot, chat_id, user_id)
    except Exception:  # noqa: BLE001 - a tag failure must never break the command
        return False, ""

    if member is None:
        return False, ""

    if isinstance(member, ChatMemberOwner):
        return False, ("ℹ️ تلگرام اجازهٔ تغییر عنوانِ «مالک گروه» را نمی‌دهد. "
                       "تگ در ربات ذخیره شد.")

    if isinstance(member, ChatMemberAdministrator):
        if not member.can_be_edited:
            return False, ("⚠️ عنوان این مدیر قابل تغییر نیست (توسط ربات ارتقا نیافته است). "
                           "برای مدیریت عنوان، ابتدا با دستور <code>ارتقا</code> او را ارتقا دهید.")
        if not await bot_has_right(bot, chat_id, "can_promote_members"):
            return False, ("⚠️ برای درج عنوان مدیر در لیست اعضا، ربات باید دسترسی "
                           "«افزودن مدیر جدید» را در تنظیمات گروه داشته باشد.")
        ok = await safe_call(
            lambda: _set_admin_title(bot, chat_id, user_id, telegram_title(tag)),
            default=None, context="admin_title", log=True)
        return ok is not None, ""

    # ------------------------------------------------ regular member / limited
    if not await bot_has_right(bot, chat_id, "can_manage_tags"):
        return False, ("⚠️ برای درج تگ در لیست اعضا، ربات باید دسترسی "
                       "«مدیریت تگ اعضا» (Manage Tags) را در تنظیمات گروه داشته باشد.\n"
                       "گروه ← تنظیمات ← مدیران ← ربات ← فعال کردن «مدیریت تگ اعضا»")
    ok = await safe_call(
        lambda: _set_member_tag(bot, chat_id, user_id, telegram_tag(tag)),
        default=None, context="member_tag", log=True)
    return ok is not None, ""
