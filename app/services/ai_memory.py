"""Seven-day, per-chat text memory for the read-only AI agent."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram.types import Message
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.timeutils import persian_datetime
from ..db.models import GroupMessageMemory

logger = logging.getLogger("armando.ai_memory")
RETENTION_DAYS = 7
MAX_CONTEXT_CHARS = 7000
MAX_RECENT_MESSAGES = 24
MAX_RELEVANT_MESSAGES = 14

_MEDIA_LABELS = (
    ("photo", "عکس"), ("video", "ویدئو"), ("animation", "گیف"),
    ("document", "فایل"), ("audio", "فایل صوتی"), ("voice", "پیام صوتی؛ بدون رونویسی"),
    ("video_note", "ویدئو پیام"), ("sticker", "استیکر"),
)
_STOP_WORDS = {
    "آرماندو", "لطفا", "لطفاً", "میشه", "میشه", "میتونی", "میتونید", "امروز",
    "دیروز", "خلاصه", "گفتگو", "گفت‌وگو", "گروه", "درباره", "چی", "چیه", "چه",
    "آخرین", "پیام", "پیامها", "پیام‌ها", "این", "اون", "برای", "لطفا", "بگو",
}


def _as_utc_naive(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _message_type_and_text(message: Message) -> tuple[str, str]:
    original = (message.text or message.caption or "").strip()
    label = next((label for field, label in _MEDIA_LABELS
                  if getattr(message, field, None) is not None), "")
    if label and original:
        return label.split("؛", 1)[0], f"[{label}] {original}"
    if label:
        return label.split("؛", 1)[0], f"[{label}]"
    if original:
        return "text", original
    if getattr(message, "location", None) is not None:
        return "location", "[موقعیت مکانی]"
    if getattr(message, "contact", None) is not None:
        return "contact", "[مخاطب اشتراک‌گذاری‌شده]"
    if getattr(message, "poll", None) is not None:
        question = getattr(message.poll, "question", "") or ""
        return "poll", f"[نظرسنجی] {question}".strip()
    if getattr(message, "venue", None) is not None:
        title = getattr(message.venue, "title", "") or ""
        return "venue", f"[مکان] {title}".strip()
    if getattr(message, "dice", None) is not None:
        return "dice", "[تاس / بازی]"
    return "other", "[پیام غیرمتنی]"


async def remember_group_message(session: AsyncSession, message: Message) -> bool:
    """Store one delivered group/channel message only when that chat opted in.

    Telegram only sends updates the bot is permitted to receive. The bot cannot
    fetch messages from before it joined, or read hidden/unknown chats.
    """
    chat = getattr(message, "chat", None)
    if chat is None or chat.type not in {"group", "supergroup", "channel"}:
        return False
    user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)

    from .chat_state import get_settings_cached

    settings = await get_settings_cached(session, int(chat.id))
    if not settings.get("ai_agent_enabled"):
        return False

    message_type, body = _message_type_and_text(message)
    if user is not None:
        user_id = int(user.id)
        speaker_name = (getattr(user, "full_name", None) or
                        getattr(user, "first_name", None) or str(user_id))
        username = str(getattr(user, "username", None) or "")
    elif sender_chat is not None:
        user_id = None
        speaker_name = (getattr(sender_chat, "title", None) or
                        getattr(sender_chat, "username", None) or "ارسال ناشناس")
        username = str(getattr(sender_chat, "username", None) or "")
    else:
        user_id = None
        speaker_name = (getattr(chat, "title", None) or
                        ("رویداد کانال" if chat.type == "channel" else "رویداد گروه"))
        username = ""

    result = await session.execute(
        select(GroupMessageMemory).where(
            GroupMessageMemory.chat_id == int(chat.id),
            GroupMessageMemory.message_id == int(message.message_id),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = GroupMessageMemory(
            chat_id=int(chat.id),
            message_id=int(message.message_id),
            created_at=_as_utc_naive(getattr(message, "date", None)),
        )
        session.add(row)
    row.user_id = user_id
    row.speaker_name = str(speaker_name)[:255]
    row.username = username[:64]
    row.message_type = message_type[:24]
    row.text = body[:5000]
    row.reply_to_message_id = (
        int(message.reply_to_message.message_id)
        if message.reply_to_message is not None else None
    )
    logger.debug("AI group-memory captured chat=%s message=%s", chat.id, message.message_id)
    return True


def _keywords(query: str) -> list[str]:
    found: list[str] = []
    for token in re.findall(r"[\w\u0600-\u06ff]{3,}", query or ""):
        token = token.strip("_-").lower()
        if token and token not in _STOP_WORDS and token not in found:
            found.append(token)
        if len(found) >= 6:
            break
    return found


def _format_row(row: GroupMessageMemory) -> str:
    speaker = html.escape(row.speaker_name or (f"کاربر {row.user_id}" if row.user_id else "ارسال ناشناس"))
    if row.username:
        speaker += f" (@{html.escape(row.username.lstrip('@'))})"
    timestamp = html.escape(persian_datetime(row.created_at))
    body = html.escape(row.text or "[پیام بدون متن]")
    return f"[{timestamp}] {speaker}: {body}"


async def group_memory_context(session: AsyncSession, *, chat_id: int,
                               before_message_id: int | None = None,
                               query: str = "") -> str:
    """Return a bounded recent + keyword-relevant window from the past seven days."""
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    base_filters = [GroupMessageMemory.chat_id == int(chat_id),
                    GroupMessageMemory.created_at >= cutoff]
    if before_message_id is not None:
        base_filters.append(GroupMessageMemory.message_id < int(before_message_id))

    recent_result = await session.execute(
        select(GroupMessageMemory).where(*base_filters)
        .order_by(GroupMessageMemory.created_at.desc(), GroupMessageMemory.id.desc())
        .limit(MAX_RECENT_MESSAGES))
    rows: dict[int, GroupMessageMemory] = {
        int(row.id): row for row in recent_result.scalars().all()
    }

    keywords = _keywords(query)
    if keywords:
        relevant_filter = or_(*[
            GroupMessageMemory.text.ilike(f"%{token.replace('%', '').replace('_', '')}%")
            for token in keywords
        ])
        relevant_result = await session.execute(
            select(GroupMessageMemory).where(*base_filters, relevant_filter)
            .order_by(GroupMessageMemory.created_at.desc(), GroupMessageMemory.id.desc())
            .limit(MAX_RELEVANT_MESSAGES))
        rows.update({int(row.id): row for row in relevant_result.scalars().all()})

    ordered = sorted(rows.values(), key=lambda row: (row.created_at, row.id))
    lines: list[str] = []
    used = 0
    for row in reversed(ordered):
        line = _format_row(row)
        addition = len(line) + (1 if lines else 0)
        if used + addition > MAX_CONTEXT_CHARS:
            if not lines:
                line = line[-MAX_CONTEXT_CHARS:]
                lines.append(line)
            break
        lines.append(line)
        used += addition
    lines.reverse()
    return "\n".join(lines)


async def purge_expired_memory(session: AsyncSession, *, now: datetime | None = None) -> int:
    cutoff = (now or datetime.utcnow()) - timedelta(days=RETENTION_DAYS)
    result = await session.execute(
        delete(GroupMessageMemory).where(GroupMessageMemory.created_at < cutoff))
    count = int(result.rowcount or 0)
    if count:
        logger.info("purged %s group-memory rows older than %s days", count, RETENTION_DAYS)
    return count


async def clear_group_memory(session: AsyncSession, chat_id: int) -> int:
    result = await session.execute(
        delete(GroupMessageMemory).where(GroupMessageMemory.chat_id == int(chat_id)))
    return int(result.rowcount or 0)
