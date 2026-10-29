"""Main group message pipeline: moderation, anti-spam and content triggers.

Pipeline order (cheapest and most important first):

1. tracking / activity
2. AFK handling
3. night mode
4. locks (including profanity & porn heuristics)
5. blacklist filters
6. anti-flood / repeat / mention spam
7. optional AI moderation
8. custom triggers (notes, personal commands, filters, magic triggers)
9. optional chat helpers and games
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_settings
from ..core.errors import safe_delete
from ..core.normalization import normalize_text
from ..db.models import User
from ..services import (
    afk as afk_service,
    antiflood,
    entertainment,
    filters as filter_service,
    gban as gban_service,
    global_mute as global_mute_service,
    locks as lock_service,
    moderation as mod,
    permissions,
    personal,
)
from ..services import notes as note_service
from ..services.chat_state import (
    get_or_create_chat,
    get_or_create_user,
    get_settings_cached,
    touch_activity,
)
from ..services.profanity import detect_porn, detect_profanity
from ..services.render import build_context, send_content
from ..services.stats import record_message
from .registry import registry

router = Router(name="messages")
logger = logging.getLogger("armando.pipeline")

HASHTAG_RE = re.compile(r"#([\w\u0600-\u06ff]{1,64})")


async def _notify(bot: Bot, chat_id: int, text: str, *, reply_to: int | None = None,
                  ttl: int = 12) -> None:
    """Send a short notice and delete it automatically."""
    try:
        message = await bot.send_message(chat_id=chat_id, text=text,
                                         reply_to_message_id=reply_to,
                                         disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        return

    # The session-level request middleware applies the fixed 30-second TTL to
    # every bot-authored message in groups, regardless of the old per-notice TTL.


@router.channel_post()
async def on_channel_post(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Offer explicit read-only AI replies in channels where the bot is an admin."""
    await get_or_create_chat(session, message.chat)
    chat_settings = await get_settings_cached(session, message.chat.id)
    from ..services.ai_chat import handle_ai_message

    await handle_ai_message(
        message, bot,
        selected_model=chat_settings.get("ai_chat_model") or None,
        session=session,
        ai_agent_enabled=bool(chat_settings.get("ai_agent_enabled")),
    )


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message, session: AsyncSession, bot: Bot) -> None:
    user = message.from_user
    chat = message.chat
    if user is None:
        return
    # Messages posted **as a channel** carry ``sender_chat``; they must reach
    # the lock engine (ordinary bot messages are still ignored).
    sender_chat = getattr(message, "sender_chat", None)
    if user.is_bot and sender_chat is None:
        return

    await get_or_create_chat(session, chat)
    await get_or_create_user(session, user)
    settings = await get_settings_cached(session, chat.id)

    text = message.text or message.caption or ""
    is_media = any([message.photo, message.video, message.document, message.audio,
                    message.voice, message.sticker, message.animation, message.video_note])
    await touch_activity(session, chat.id, user.id, media=is_media)
    await record_message(session, chat.id, user.id, media=is_media)

    # ------------------------------------------------- global ban (owner level)
    if app_settings.global_ban_enabled:
        gb_reason = await gban_service.is_banned(session, user.id)
        if gb_reason is not None:
            await gban_service.enforce(
                bot, session, chat_id=chat.id, chat_title=chat.title or "",
                user_id=user.id, user_name=user.full_name or "", reason=gb_reason,
                message_id=message.message_id, clean=True)
            return

    # A global mute is persistent and enforced independently of group settings.
    # It is applied only in registered supergroups where the owner is present
    # and the bot itself has admin permission to restrict members.
    global_mute_reason = await global_mute_service.is_muted(session, user.id)
    if global_mute_reason is not None:
        enforced = await global_mute_service.enforce(
            bot, session, chat_id=chat.id, user_id=user.id,
            user_name=user.full_name or "", reason=global_mute_reason,
            message_id=message.message_id)
        if enforced:
            return

    actor = await permissions.build_actor(bot, chat.id, user.id, session=session)
    if sender_chat is not None:
        # A channel post is never an administrator action: treat the sender as
        # an ordinary member so the lock engine may act on it.
        actor = permissions.Actor(user_id=user.id, chat_id=chat.id, telegram_status="member",
                                  rights_known=False)

    # ------------------------------------------------------------------ AFK
    if settings.get("afk_enabled", True):
        was_afk, _, _ = await afk_service.get_afk(session, user.id)
        if was_afk and text and registry.match(
                text, has_reply=message.reply_to_message is not None) is None:
            await afk_service.clear_afk(session, user.id)
            await _notify(bot, chat.id, "👋 به جمع برگشتید.", reply_to=message.message_id)
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
            if target_id != user.id:
                is_afk, reason, since = await afk_service.get_afk(session, target_id)
                if is_afk:
                    target_user = await session.get(User, target_id)
                    name = (target_user.first_name if target_user else None) or "کاربر"
                    await _notify(bot, chat.id, afk_service.afk_notice(name, reason, since),
                                  reply_to=message.message_id, ttl=20)

    # ------------------------------------------------------------ night mode
    if settings.get("night_mode_enabled"):
        from ..services import nightmode
        from ..services.chat_state import get_settings

        settings_obj = await get_settings(session, chat.id)
        if not nightmode.bypasses(actor, settings_obj):
            enforced = await nightmode.enforce(bot, session, message=message, actor=actor,
                                               settings_obj=settings_obj,
                                               chat_title=chat.title or "")
            if enforced:
                await _notify(bot, chat.id,
                              "🌙 در ساعات حالت شب، ارسال پیام محدود است.", ttl=15)
                return

    # ------------------------------------------------------- locks & content
    bypass_locks = actor.bypasses("locks")
    if not bypass_locks:
        locks = await lock_service.get_locks_map(session, chat.id)
        if (sender_chat is not None and sender_chat.id == chat.id
                and settings.get("anonymous_sender_exempt", True)):
            # This is the explicitly selected default: exempt only a post made
            # as this exact group, not a foreign channel identity.
            locks = dict(locks)
            locks.pop("anonymous", None)
        violation = await lock_service.check_message(message, locks)
        if violation is None and settings.get("anti_profanity_enabled"):
            matches = detect_profanity(text)
            if matches:
                violation = lock_service.Violation(
                    key="profanity", label="فحاشی", emoji="🤬",
                    action=settings.get("anti_profanity_action", "delete_warn"),
                    duration=None)
        if violation is None and settings.get("anti_porn_enabled"):
            file_name = None
            if message.document:
                file_name = message.document.file_name
            elif message.video:
                file_name = message.video.file_name
            porn_matches = detect_porn(text, file_name=file_name,
                                       strictness=int(settings.get("anti_porn_strictness", 2)))
            visual_match = False
            if not porn_matches and any((message.photo, message.video, message.animation,
                                         message.video_note, message.document)):
                try:
                    from ..services.media_safety import detect_visual_nudity

                    visual_match = await detect_visual_nudity(bot, message)
                except Exception as exc:  # noqa: BLE001 - optional vision must never crash moderation
                    logger.warning("بررسی تصویری ضدپورن ناموفق بود: %s", exc.__class__.__name__)
            if porn_matches or visual_match:
                violation = lock_service.Violation(
                    key="porn", label="محتوای مستهجن", emoji="🔞",
                    action=settings.get("anti_porn_action", "delete_ban"),
                    duration=None)
        if violation is not None:
            actor_name = user.full_name or "کاربر"
            result = await lock_service.apply_violation(
                bot, session, message=message, violation=violation,
                chat_title=chat.title or "", reason=f"نقض قفل «{violation.label}»")
            await _notify(bot, chat.id,
                          f"{violation.emoji} پیام {actor_name} به دلیل «{violation.label}» حذف شد.",
                          ttl=12)
            if "حذف" not in result:
                logger.debug("lock action result: %s", result)
            return
        if sender_chat is not None:
            return  # nothing else applies to a channel post

    # ------------------------------------------------------------- blocklist
    if not actor.bypasses("filters"):
        rules = await filter_service.get_rules(session, chat.id)
        matched = filter_service.find_blocklist_match(text, rules) if text else None
        if matched is not None:
            await filter_service.bump_hits(session, matched.id)
            bot_actor = await permissions.get_bot_actor(bot, chat.id)
            await mod.apply_action(bot, session, chat_id=chat.id, target_id=user.id,
                                   actor=bot_actor, action=matched.action,
                                   duration=matched.duration, target_name=user.full_name or "",
                                   message_id=message.message_id, chat_title=chat.title or "",
                                   reason=f"کلمه فیلتر شده: {matched.trigger}", source="filter")
            await _notify(bot, chat.id,
                          f"🚫 پیام به دلیل استفاده از کلمه فیلتر شده حذف شد.", ttl=10)
            return

    # ------------------------------------------------------------- anti-spam
    antiflood.tracker.record(chat.id, user.id, text=text, media=is_media)
    if settings.get("antispam_enabled") and not actor.bypasses("antiflood"):
        verdict = antiflood.evaluate(
            chat.id, user.id, text=text, media=is_media,
            antiflood_enabled=bool(settings.get("antiflood_enabled")),
            flood_limit=int(settings.get("antiflood_count", 5) or 0),
            flood_window=int(settings.get("antiflood_window", 3) or 3),
            repeat_limit=int(settings.get("antispam_same_limit", 0) or 0),
            mention_limit=int(settings.get("mention_spam_limit", 0) or 0),
        )
        if verdict.spam:
            action = settings.get("antiflood_action", "mute")
            bot_actor = await permissions.get_bot_actor(bot, chat.id)
            await mod.apply_action(bot, session, chat_id=chat.id, target_id=user.id,
                                   actor=bot_actor, action=action, target_name=user.full_name or "",
                                   message_id=message.message_id, chat_title=chat.title or "",
                                   reason=verdict.label_fa, source="antispam")
            antiflood.tracker.reset_user(chat.id, user.id)
            await _notify(bot, chat.id, f"⚠️ رفتار اسپم تشخیص داده شد: {verdict.label_fa}", ttl=12)
            return

    # --------------------------------------------------------- AI moderation
    if settings.get("ai_enabled"):
        from ..services.ai import ai_moderator

        verdict = await ai_moderator.analyze(text)
        if verdict is not None and verdict.is_violation:
            bot_actor = await permissions.get_bot_actor(bot, chat.id)
            await mod.apply_action(bot, session, chat_id=chat.id, target_id=user.id,
                                   actor=bot_actor, action="delete_warn",
                                   target_name=user.full_name or "",
                                   message_id=message.message_id, chat_title=chat.title or "",
                                   reason=f"تشخیص هوش مصنوعی ({verdict.reason or 'محتوای نامناسب'})",
                                   source="ai")
            await _notify(bot, chat.id, "🤖 پیام توسط سیستم هوشمند حذف شد.", ttl=10)
            return

    # ---------------------------------------------------- optional chat helpers
    from ..services.instagram import handle_instagram_message

    if await handle_instagram_message(message, bot):
        return
    from ..services.ai_chat import handle_ai_message

    if await handle_ai_message(
            message, bot,
            selected_model=settings.get("ai_chat_model") or None,
            session=session,
            ai_agent_enabled=bool(settings.get("ai_agent_enabled"))):
        return

    # ------------------------------------------------------- content triggers
    if await _handle_triggers(message, session, bot, text, chat.id, user):
        return

    # ----------------------------------------------------------- guess game
    if text.strip().isdigit() or (normalize_text(text, mode="command").strip().isdigit()):
        from ..core.normalization import normalize_digits

        digits = normalize_digits(text.strip(), to="ascii")
        if digits.isdigit():
            result, _ = entertainment.guess_game.guess(chat.id, user.id, int(digits))
            if "ابتدا" not in result and "تمام شد" not in result:
                await _notify(bot, chat.id, result, reply_to=message.message_id, ttl=20)
                return


async def _handle_triggers(message: Message, session: AsyncSession, bot: Bot, text: str,
                           chat_id: int, user) -> bool:
    """Notes / personal commands / custom filters / magic triggers."""
    # Magic sticker / GIF triggers
    if message.sticker:
        trigger = await personal.get_magic(session, chat_id, "sticker",
                                           message.sticker.file_unique_id)
        if trigger:
            context = await build_context(session, bot, chat_id, user.id, user=user)
            await send_content(bot, chat_id, content_type=trigger.content_type,
                               text=trigger.text, file_id=trigger.file_id,
                               caption=trigger.caption, buttons=trigger.buttons,
                               context=context, reply_to_message_id=message.message_id)
            return True
    if message.animation:
        trigger = await personal.get_magic(session, chat_id, "gif",
                                           message.animation.file_unique_id)
        if trigger:
            context = await build_context(session, bot, chat_id, user.id, user=user)
            await send_content(bot, chat_id, content_type=trigger.content_type,
                               text=trigger.text, file_id=trigger.file_id,
                               caption=trigger.caption, buttons=trigger.buttons,
                               context=context, reply_to_message_id=message.message_id)
            return True

    if not text:
        return False

    # #hashtag notes
    for match in HASHTAG_RE.findall(text):
        note = await note_service.get_note(session, chat_id, match)
        if note is not None:
            context = await build_context(session, bot, chat_id, user.id, user=user)
            await send_content(bot, chat_id, content_type=note.content_type, text=note.text,
                               file_id=note.file_id, caption=note.caption,
                               buttons=note.buttons, context=context,
                               reply_to_message_id=message.message_id)
            return True

    normalized = normalize_text(text, mode="command").strip()

    # personal commands
    command_row = await personal.get_command(session, chat_id, normalized)
    if command_row is not None:
        if personal.on_cooldown(chat_id, command_row.trigger, int(command_row.cooldown or 0)):
            return True
        context = await build_context(session, bot, chat_id, user.id, user=user)
        await send_content(bot, chat_id, content_type=command_row.content_type,
                           text=command_row.text, file_id=command_row.file_id,
                           caption=command_row.caption, buttons=command_row.buttons,
                           context=context, reply_to_message_id=message.message_id)
        return True

    # custom filters (non blocklist)
    rules = await filter_service.get_rules(session, chat_id)
    custom = filter_service.find_custom_match(text, rules)
    if custom is not None:
        if personal.on_cooldown(chat_id, custom.trigger, int(custom.cooldown or 0)):
            return True
        await filter_service.bump_hits(session, custom.id)
        context = await build_context(session, bot, chat_id, user.id, user=user)
        payload_text = custom.response_text
        if custom.replies:
            import random

            payload_text = random.choice(list(custom.replies))
        await send_content(bot, chat_id, content_type=custom.response_type,
                           text=payload_text, file_id=custom.response_file_id,
                           caption=custom.response_caption, buttons=custom.response_buttons,
                           context=context, reply_to_message_id=message.message_id)
        return True

    # named note retrieval: "گرفتن X" is a command; also allow bare note names
    note = await note_service.get_note(session, chat_id, normalized)
    if note is not None and len(normalized.split()) <= 2:
        context = await build_context(session, bot, chat_id, user.id, user=user)
        await send_content(bot, chat_id, content_type=note.content_type, text=note.text,
                           file_id=note.file_id, caption=note.caption, buttons=note.buttons,
                           context=context, reply_to_message_id=message.message_id)
        return True
    return False
