"""Granular lock engine.

Every lock has its own ON/OFF state, its own punishment and (optionally) a
threshold.  Detection is purely local: it only uses information that the
Telegram Bot API actually exposes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.errors import safe_delete
from ..core.normalization import (
    count_emojis,
    count_mentions,
    has_chinese,
    has_cyrillic,
    has_devanagari,
    has_hashtag,
    has_latin,
    has_persian,
    has_phone,
    has_telegram_link,
    has_url,
    normalize_text,
)
from .profanity import detect_porn, detect_profanity

logger = logging.getLogger("armando.locks")


@dataclass
class LockSpec:
    key: str
    label_fa: str
    emoji: str
    default_action: str = "delete"
    threshold: bool = False
    threshold_field: str | None = None
    default_threshold: int = 5
    group: str = "content"
    detector: Callable[[Message, "LockState"], bool] | None = None


@dataclass
class LockState:
    enabled: bool = False
    action: str = "delete"
    duration: int | None = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
def _text_of(message: Message) -> str:
    return message.text or message.caption or ""


def _d_link(m, s: LockState) -> bool:
    return has_url(_text_of(m)) or bool(m.reply_markup and _markup_urls(m))


def _markup_urls(message: Message) -> bool:
    markup = message.reply_markup
    if not markup:
        return False
    for row in getattr(markup, "inline_keyboard", []) or []:
        for button in row:
            if getattr(button, "url", None):
                return True
    return False


def _d_username(m, s: LockState) -> bool:
    return "@" in _text_of(m)


def _d_mention(m, s: LockState) -> bool:
    text = _text_of(m)
    if count_mentions(text) > 0:
        return True
    return any(e.type in {"mention", "text_mention"} for e in (m.entities or []))


def _d_hashtag(m, s: LockState) -> bool:
    return has_hashtag(_text_of(m))


def _d_forward(m, s: LockState) -> bool:
    return any([getattr(m, "forward_origin", None), getattr(m, "forward_from", None),
                getattr(m, "forward_from_chat", None), getattr(m, "forward_date", None)])


def _d_bot(m, s: LockState) -> bool:
    user = m.from_user
    return bool(user and user.is_bot) or bool(getattr(m, "via_bot", None))


def _d_photo(m, s: LockState) -> bool:
    return bool(m.photo)


def _d_video(m, s: LockState) -> bool:
    return bool(m.video) or bool(getattr(m, "video_note", None)) is False and bool(m.video)


def _d_video_note(m, s: LockState) -> bool:
    return bool(m.video_note)


def _d_voice(m, s: LockState) -> bool:
    return bool(m.voice)


def _d_audio(m, s: LockState) -> bool:
    return bool(m.audio)


def _d_sticker(m, s: LockState) -> bool:
    return bool(m.sticker)


def _d_gif(m, s: LockState) -> bool:
    return bool(m.animation)


def _d_document(m, s: LockState) -> bool:
    return bool(m.document)


def _d_contact(m, s: LockState) -> bool:
    return bool(m.contact)


def _d_location(m, s: LockState) -> bool:
    return bool(m.location) or bool(getattr(m, "venue", None))


def _d_poll(m, s: LockState) -> bool:
    return bool(m.poll)


def _d_dice(m, s: LockState) -> bool:
    return bool(m.dice)


def _d_game(m, s: LockState) -> bool:
    return bool(getattr(m, "game", None))


def _d_invoice(m, s: LockState) -> bool:
    return bool(getattr(m, "invoice", None)) or bool(getattr(m, "successful_payment", None))


def _d_story(m, s: LockState) -> bool:
    return bool(getattr(m, "story", None))


def _d_inline(m, s: LockState) -> bool:
    return bool(getattr(m, "via_bot", None))


def _d_command(m, s: LockState) -> bool:
    text = _text_of(m).strip()
    return text.startswith("/") or (len(text) > 1 and text.startswith("!"))


def _d_phone(m, s: LockState) -> bool:
    return has_phone(_text_of(m))


def _d_persian(m, s: LockState) -> bool:
    return has_persian(_text_of(m))


def _d_english(m, s: LockState) -> bool:
    return has_latin(_text_of(m))


def _d_cyrillic(m, s: LockState) -> bool:
    return has_cyrillic(_text_of(m))


def _d_emoji(m, s: LockState) -> bool:
    limit = int((s.extra or {}).get("max_emoji", 5))
    return count_emojis(_text_of(m)) > limit


def _d_mention_spam(m, s: LockState) -> bool:
    limit = int((s.extra or {}).get("max_mentions", 3))
    return count_mentions(_text_of(m)) > limit


def _d_long(m, s: LockState) -> bool:
    limit = int((s.extra or {}).get("max_length", 1000))
    return len(_text_of(m)) > limit


def _d_chinese(m, s: LockState) -> bool:
    return has_chinese(_text_of(m))


def _d_hindi(m, s: LockState) -> bool:
    return has_devanagari(_text_of(m))


def _d_russian(m, s: LockState) -> bool:
    return has_cyrillic(_text_of(m))


def _d_anonymous(m, s: LockState) -> bool:
    """Messages posted under a chat/channel identity in Telegram."""
    return getattr(m, "sender_chat", None) is not None


def _d_reply(m, s: LockState) -> bool:
    return m.reply_to_message is not None


def _d_buttons(m, s: LockState) -> bool:
    markup = m.reply_markup
    if not markup:
        return False
    if getattr(markup, "inline_keyboard", None):
        return True
    if getattr(markup, "keyboard", None):
        return True
    return False


def _d_profanity(m, s: LockState) -> bool:
    return bool(detect_profanity(_text_of(m)))


def _d_porn(m, s: LockState) -> bool:
    file_name = None
    if m.document:
        file_name = m.document.file_name
    elif m.video:
        file_name = m.video.file_name
    matches = detect_porn(_text_of(m), file_name=file_name,
                          strictness=int((s.extra or {}).get("strictness", 2)))
    return len(matches) > 0


def _d_telegram_link(m, s: LockState) -> bool:
    return has_telegram_link(_text_of(m)) or _markup_urls(m)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def _spec(key: str, label_fa: str, emoji: str, detector: Callable, *,
          default_action: str = "delete", threshold: bool = False,
          threshold_field: str | None = None, default_threshold: int = 5,
          group: str = "content") -> LockSpec:
    return LockSpec(key=key, label_fa=label_fa, emoji=emoji, detector=detector,
                    default_action=default_action, threshold=threshold,
                    threshold_field=threshold_field, default_threshold=default_threshold,
                    group=group)


LOCK_REGISTRY: dict[str, LockSpec] = {
    # ---- links / references
    "links": _spec("links", "لینک", "🔗", _d_link, default_action="delete_warn"),
    "telegram_links": _spec("telegram_links", "لینک تلگرام", "🧷", _d_telegram_link,
                            default_action="delete_warn"),
    "usernames": _spec("usernames", "نام کاربری (@)", "🆔", _d_username),
    "mentions": _spec("mentions", "منشن", "💬", _d_mention),
    "hashtags": _spec("hashtags", "هشتگ", "#️⃣", _d_hashtag),
    "forward": _spec("forward", "فوروارد", "📨", _d_forward),
    "reply": _spec("reply", "ریپلای", "↩️", _d_reply),
    "buttons": _spec("buttons", "دکمه شیشه‌ای", "🎛", _d_buttons),
    "inline": _spec("inline", "محتوای اینلاین", "🧩", _d_inline),
    # ---- media
    "photo": _spec("photo", "عکس", "🖼", _d_photo, group="media"),
    "video": _spec("video", "ویدیو", "🎞", _d_video, group="media"),
    "video_note": _spec("video_note", "ویدیو پیام", "🎥", _d_video_note, group="media"),
    "voice": _spec("voice", "ویس", "🎙", _d_voice, group="media"),
    "audio": _spec("audio", "آهنگ", "🎵", _d_audio, group="media"),
    "sticker": _spec("sticker", "استیکر", "🎴", _d_sticker, group="media"),
    "gif": _spec("gif", "گیف", "🌀", _d_gif, group="media"),
    "document": _spec("document", "فایل", "📁", _d_document, group="media"),
    "contact": _spec("contact", "مخاطب", "📇", _d_contact, group="media"),
    "location": _spec("location", "موقعیت مکانی", "📍", _d_location, group="media"),
    "poll": _spec("poll", "نظرسنجی", "📊", _d_poll, group="media"),
    "dice": _spec("dice", "تاس و دارت", "🎲", _d_dice, group="media"),
    "game": _spec("game", "بازی", "🎮", _d_game, group="media"),
    "invoice": _spec("invoice", "پرداخت", "💳", _d_invoice, group="media"),
    "story": _spec("story", "استوری", "🕘", _d_story, group="media"),
    # ---- behaviour / text
    "bots": _spec("bots", "ربات‌ها", "🤖", _d_bot, group="text"),
    "command": _spec("command", "دستورات (/ )", "⌨️", _d_command, group="text"),
    "phone": _spec("phone", "شماره تلفن", "☎️", _d_phone, group="text"),
    "persian": _spec("persian", "متن فارسی", "🇮🇷", _d_persian, group="text"),
    "english": _spec("english", "متن انگلیسی", "🔤", _d_english, group="text"),
    "cyrillic": _spec("cyrillic", "متن سیریلیک", "🔠", _d_cyrillic, group="text"),
    "profanity": _spec("profanity", "فحاشی", "🤬", _d_profanity, default_action="delete_warn",
                       group="text"),
    "porn": _spec("porn", "محتوای مستهجن", "🔞", _d_porn, default_action="delete_ban", group="text"),
    "emoji": _spec("emoji", "ایموجی زیاد", "😀", _d_emoji, threshold=True,
                   threshold_field="max_emoji", default_threshold=5, group="text"),
    "mention_spam": _spec("mention_spam", "منشن زیاد", "📣", _d_mention_spam, threshold=True,
                          threshold_field="max_mentions", default_threshold=3, group="text"),
    "long": _spec("long", "پیام طولانی", "📏", _d_long, default_action="delete_mute", threshold=True,
                  threshold_field="max_length", default_threshold=1000, group="text"),
    # ---- language locks (requested: Chinese / Russian / Hindi)
    "chinese": _spec("chinese", "زبان چینی", "🇨🇳", _d_chinese, default_action="delete_mute",
                     group="text"),
    "russian": _spec("russian", "زبان روسی", "🇷🇺", _d_russian, default_action="delete_mute",
                     group="text"),
    "hindi": _spec("hindi", "زبان هندی", "🇮🇳", _d_hindi, default_action="delete_mute",
                   group="text"),
    # ---- anonymous senders (channel posts / hidden admins)
    "anonymous": _spec("anonymous", "ارسال با کانال/ناشناس", "🕵️", _d_anonymous,
                       default_action="delete_mute", group="text"),
}

LOCK_GROUPS: dict[str, str] = {
    "links": "🔗 لینک و ارجاع",
    "media": "🖼 رسانه",
    "text": "🔤 متن و رفتار",
}

# Locks that are also exposed through the settings dashboard quick toggles.
QUICK_LOCKS: tuple[str, ...] = ("links", "profanity", "porn", "forward", "photo", "video",
                                "sticker", "gif", "voice", "bots", "english", "mention_spam")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
async def get_locks_map(session: AsyncSession, chat_id: int) -> dict[str, LockState]:
    """Cached mapping of lock key -> state for the hot message pipeline."""
    cached = cache.lock_cache.get(chat_id)
    if cached is not None:
        return cached
    from ..db.models import Lock

    result = await session.execute(select(Lock).where(Lock.chat_id == chat_id))
    mapping: dict[str, LockState] = {}
    for row in result.scalars().all():
        mapping[row.key] = LockState(enabled=bool(row.enabled), action=row.action,
                                     duration=row.duration, extra=row.extra or {})
    cache.lock_cache.set(chat_id, mapping)
    return mapping


def invalidate_locks(chat_id: int) -> None:
    cache.lock_cache.delete(chat_id)


async def set_lock(session: AsyncSession, chat_id: int, key: str, enabled: bool,
                   action: str | None = None, duration: int | None = None,
                   extra: dict | None = None, updated_by: int | None = None) -> LockState:
    from ..db.models import Lock

    if key not in LOCK_REGISTRY:
        raise KeyError(f"unknown lock: {key}")
    from .chat_state import ensure_chat

    await ensure_chat(session, chat_id)
    result = await session.execute(select(Lock).where(Lock.chat_id == chat_id, Lock.key == key))
    row = result.scalar_one_or_none()
    spec = LOCK_REGISTRY[key]
    if row is None:
        row = Lock(chat_id=chat_id, key=key, enabled=enabled,
                   action=action or spec.default_action, duration=duration,
                   extra=extra or {}, updated_by=updated_by)
        session.add(row)
    else:
        row.enabled = enabled
        if action is not None:
            row.action = action
        if duration is not None or action in {"mute", "ban"}:
            row.duration = duration
        if extra is not None:
            merged = dict(row.extra or {})
            merged.update(extra)
            row.extra = merged
        row.updated_by = updated_by
        row.updated_at = row.updated_at
    await session.flush()
    invalidate_locks(chat_id)
    return LockState(enabled=bool(row.enabled), action=row.action, duration=row.duration,
                     extra=row.extra or {})


async def toggle_lock(session: AsyncSession, chat_id: int, key: str,
                      updated_by: int | None = None) -> LockState:
    locks = await get_locks_map(session, chat_id)
    state = locks.get(key)
    return await set_lock(session, chat_id, key, not (state.enabled if state else False),
                          action=(state.action if state else None),
                          duration=(state.duration if state else None),
                          updated_by=updated_by)


async def clear_locks(session: AsyncSession, chat_id: int) -> int:
    from ..db.models import Lock

    result = await session.execute(delete(Lock).where(Lock.chat_id == chat_id))
    invalidate_locks(chat_id)
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Detection entry point
# --------------------------------------------------------------------------- #
@dataclass
class Violation:
    key: str
    label: str
    emoji: str
    action: str
    duration: int | None


async def check_message(message: Message, locks: dict[str, LockState]) -> Optional[Violation]:
    """Return the first lock violation of ``message`` (or ``None``)."""
    for key, state in locks.items():
        if not state.enabled:
            continue
        spec = LOCK_REGISTRY.get(key)
        if spec is None or spec.detector is None:
            continue
        try:
            if spec.detector(message, state):
                return Violation(key=key, label=spec.label_fa, emoji=spec.emoji,
                                 action=state.action or spec.default_action,
                                 duration=state.duration)
        except Exception as exc:  # noqa: BLE001 - a broken detector must not break the bot
            logger.warning("lock detector failed key=%s error=%s", key, exc)
    return None


def _is_anonymous_admin(user) -> bool:
    from .permissions import ANONYMOUS_ADMIN_IDS

    return user.id in ANONYMOUS_ADMIN_IDS or bool(getattr(user, "is_anonymous_admin", False))


async def _punish_sender_chat(bot: Bot, session: AsyncSession, *, chat_id: int,
                              sender_chat_id: int, name: str, action: str,
                              duration: int | None, message_id: int | None,
                              reason: str = "") -> str:
    """Delete + (temporarily) ban a channel that posts into the group."""
    from datetime import datetime, timedelta

    from ..core.errors import safe_call, safe_delete

    await safe_delete(bot, chat_id, message_id, context="lock_delete")
    action = (action or "delete").lower().replace("delete_", "")
    until_date: datetime | None = None
    if action in {"mute", "temp_mute"}:
        # A channel cannot be "restricted": a temporary ban is the equivalent.
        until_date = datetime.utcnow() + timedelta(seconds=int(duration or 3600))
    elif action in {"ban", "temp_ban"} and duration:
        until_date = datetime.utcnow() + timedelta(seconds=int(duration))
    elif action not in {"ban", "temp_ban", "kick", "mute", "temp_mute"}:
        return "🗑 پیام حذف شد."

    banned = await safe_call(
        lambda: bot.ban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id,
                                         until_date=until_date),
        default=None, context="ban_sender_chat", log=True)
    if banned is None:
        return "🗑 پیام حذف شد (امکان مسدود کردن کانال وجود نداشت)."
    if action == "kick":
        await safe_call(
            lambda: bot.unban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id),
            default=None, context="unban_sender_chat")
        return f"📢 ارسال‌کنندهٔ ناشناس «{name}» از گروه اخراج شد."
    label = "مسدود" if until_date is None else "موقتاً مسدود"
    return f"📢 ارسال‌کنندهٔ ناشناس «{name}» {label} شد."


async def apply_violation(bot: Bot, session: AsyncSession, *, message: Message,
                          violation: Violation, chat_title: str = "", reason: str = "") -> str:
    """Punish a lock violation using the shared action engine."""
    from .moderation import apply_action
    from .permissions import get_bot_actor

    actor = await get_bot_actor(bot, message.chat.id)
    user = message.from_user

    # A channel (or a hidden admin) cannot be "restricted": the only thing
    # Telegram offers is banChatSenderChat, so use it for every punishment
    # that is stronger than a simple delete.
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        if sender_chat.id == message.chat.id:
            # Never ban the group itself. Telegram does not expose which human
            # administrator authored an anonymous group post.
            await safe_delete(bot, message.chat.id, message.message_id, context="lock_delete")
            return "🗑 پیام با نام گروه حذف شد؛ هویت مدیر فرستنده برای ربات قابل مشاهده نیست."
        return await _punish_sender_chat(bot, session, chat_id=message.chat.id,
                                         sender_chat_id=sender_chat.id,
                                         name=getattr(sender_chat, "title", "") or "کانال",
                                         action=violation.action, duration=violation.duration,
                                         message_id=message.message_id, reason=reason)
    if user is not None and _is_anonymous_admin(user):
        # Anonymous group admins cannot be muted by the bot either.
        await safe_delete(bot, message.chat.id, message.message_id, context="lock_delete")
        return "🗑 پیام ناشناس حذف شد (مدیر ناشناس قابل محدود کردن نیست)."

    target_id = user.id if user else message.chat.id
    target_name = user.full_name if user else "ناشناس"
    return await apply_action(
        bot, session, chat_id=message.chat.id, target_id=target_id, actor=actor,
        action=violation.action, duration=violation.duration, target_name=target_name,
        message_id=message.message_id, chat_title=chat_title,
        reason=reason or f"نقض قفل «{violation.label}»", source="lock",
    )


def lock_status_line(key: str, state: LockState | None) -> str:
    spec = LOCK_REGISTRY.get(key)
    if not spec:
        return key
    enabled = bool(state and state.enabled)
    icon = "🟢" if enabled else "🔴"
    return f"{spec.emoji} {spec.label_fa}  {icon}"


def normalize_lock_key(text: str) -> str | None:
    """Resolve a Persian lock name typed by an admin to a registry key."""
    raw = normalize_text(text or "", mode="command").strip()
    if not raw:
        return None
    if raw in LOCK_REGISTRY:
        return raw
    aliases = {
        "لینک": "links", "لینکها": "links", "لینک ها": "links", "لینک تلگرام": "telegram_links",
        "تلگرام": "telegram_links", "یوزرنیم": "usernames", "نام کاربری": "usernames",
        "آیدی": "usernames", "منشن": "mentions", "تگ": "mentions", "هشتگ": "hashtags",
        "فوروارد": "forward", "فروارد": "forward", "ریپلای": "reply", "دکمه": "buttons",
        "اینلاین": "inline", "عکس": "photo", "تصویر": "photo", "ویدیو": "video",
        "فیلم": "video", "ویس": "voice", "صدا": "voice", "آهنگ": "audio", "موزیک": "audio",
        "استیکر": "sticker", "گیف": "gif", "فایل": "document", "داکیومنت": "document",
        "مخاطب": "contact", "موقعیت": "location", "لوکیشن": "location",
        "نظرسنجی": "poll", "تاس": "dice", "بازی": "game", "پرداخت": "invoice",
        "استوری": "story", "ربات": "bots", "رباتها": "bots", "دستور": "command",
        "تلفن": "phone", "شماره": "phone", "فارسی": "persian", "انگلیسی": "english",
        "سیریلیک": "cyrillic", "روسی": "russian", "روس": "russian", "چینی": "chinese",
        "چین": "chinese", "کانتیز": "chinese", "هندی": "hindi", "هند": "hindi",
        "ناشناس": "anonymous", "کانال": "anonymous", "ارسال با کانال": "anonymous",
        "فحش": "profanity", "فحاشی": "profanity",
        "پورن": "porn", "مستهجن": "porn", "ایموجی": "emoji", "منشن زیاد": "mention_spam",
        "طولانی": "long", "ویدیوپیام": "video_note", "ویس پیام": "video_note",
    }
    normalized_aliases = {normalize_text(k, mode="command"): v for k, v in aliases.items()}
    return normalized_aliases.get(raw)
