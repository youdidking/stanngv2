"""Join / leave / service message handling, anti-raid and CAPTCHA callbacks."""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message,
                           MessageReactionUpdated)
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_settings
from ..core.errors import safe_delete, safe_restrict, safe_send, schedule_delete
from ..core.message_lifecycle import GROUP_BOT_MESSAGE_TTL
from ..core.normalization import to_persian_digits
from ..db.models import Chat
from ..services import antiraid, captcha, connection as connection_service
from ..services import gban as gban_service
from ..services import global_mute as global_mute_service
from ..services import leave_guard
from ..services import roster as roster_service
from ..services import moderation as mod
from ..services import welcome as welcome_service
from ..services.chat_state import (get_or_create_chat, get_or_create_user, get_settings,
                                   get_settings_cached)
from ..services.moderation import MUTE_PERMISSIONS

router = Router(name="members")
logger = logging.getLogger("armando.members")


async def _raid_check(message: Message, session: AsyncSession, bot: Bot, joined) -> None:
    settings_obj = await get_settings(session, message.chat.id)
    if not settings_obj.antiraid_enabled:
        return
    count = antiraid.raid_tracker.record_join(message.chat.id, joined.id)
    if antiraid.raid_tracker.is_raid(message.chat.id, int(settings_obj.antiraid_threshold or 8),
                                     int(settings_obj.antiraid_window or 60)):
        if not antiraid.raid_tracker.is_active(message.chat.id):
            antiraid.raid_tracker.activate(message.chat.id, 900)
            antiraid.raid_tracker.mark_alerted(message.chat.id)
            await safe_send(
                bot, message.chat.id,
                "🛡 <b>هشدار Raid</b>\n\n"
                f"در {to_persian_digits(str(settings_obj.antiraid_window))} ثانیه "
                f"{to_persian_digits(str(settings_obj.antiraid_threshold))} کاربر وارد گروه شدند.\n"
                "حالت محافظت به‌طور خودکار فعال شد.",
                parse_mode="HTML")


async def _joined_at(session: AsyncSession, chat_id: int, user_id: int):
    """When did this member join? (needed by the quick-leave rule)."""
    from ..db.models import ChatMemberState
    from ..services.chat_state import get_member_state

    state = await get_member_state(session, chat_id, user_id, create=False)
    return getattr(state, "joined_at", None) if state is not None else None


async def _leave_guard(bot: Bot, session: AsyncSession, chat_id: int, user,
                       settings: dict, joined_at,
                       member_status: str | None = None, chat_title: str = ""):
    """Ban members who leave; return the reason and the group notice message."""
    if not leave_guard.is_enabled(settings):
        return None, None
    reason = await leave_guard.check_leave(
        bot, chat_id, user.id, settings, joined_at, session=session,
        member_status=member_status, chat_title=chat_title)
    if reason is None:
        return None, None
    from ..keyboards.factory import cb, markup, primary, row, success

    rule = "ban_on_leave" if settings.get("ban_on_leave") else "quick_leave_ban"
    name = html.escape((user.full_name or "").strip() or str(user.id))
    text = ("🚪 <b>قفل خروج</b>\n\n"
            f"<a href=\"tg://user?id={user.id}\">{name}</a> گروه را ترک کرد و "
            f"طبق تنظیمات بن شد.\n"
            f"📌 دلیل: {reason}")
    keyboard = markup([
        [success("↩️ رفع بن", cb("undo", "unban", user.id)),
         primary("⛔️ خاموش کردن این قانون", cb("lg", "off", rule))],
        row(primary("🏠 پنل", cb("nav", "home"))),
    ])
    notice = await safe_send(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    return reason, notice


@router.message(F.new_chat_members)
async def on_new_members(message: Message, session: AsyncSession, bot: Bot) -> None:
    chat = await get_or_create_chat(session, message.chat)
    settings_obj = await get_settings(session, message.chat.id)

    for user in message.new_chat_members:
        if user.id == bot.id:
            await safe_send(bot, message.chat.id,
                            "🤖 <b>Armando Group Manager</b> به گروه اضافه شد.\n"
                            "برای شروع، ربات را ادمین کنید و سپس دستور <code>پنل</code> را بزنید.",
                            parse_mode="HTML")
            chat.bot_is_admin = True
            continue

        await get_or_create_user(session, user)

        # Sticky ban: an active ban blocks every re-entry attempt.
        chat_guard = await get_settings_cached(session, message.chat.id)
        if chat_guard.get("sticky_ban", True) and await mod.enforce_sticky_ban(
                bot, session, message.chat.id, user.id):
            await safe_send(
                bot, message.chat.id,
                f"🔒 <a href=\"tg://user?id={user.id}\">{user.full_name or user.id}</a> "
                f"بن است و اجازه ورود ندارد.\n"
                f"برای بازگشت، یک مدیر باید دستور <code>رفع بن</code> را بزند.",
                parse_mode="HTML")
            if settings_obj.clean_join:
                await safe_delete(bot, message.chat.id, message.message_id,
                                  context="sticky_ban_join")
            continue

        await welcome_service.record_join(session, message.chat.id, user)
        await _raid_check(message, session, bot, user)

        # Global ban (owner feature, OFF by default) - works in groups too
        if app_settings.global_ban_enabled:
            gb_reason = await gban_service.is_banned(session, user.id)
            if gb_reason is not None:
                await gban_service.enforce(
                    bot, session, chat_id=message.chat.id,
                    chat_title=message.chat.title or "", user_id=user.id,
                    user_name=user.full_name or "", reason=gb_reason,
                    message_id=message.message_id,
                    clean=bool(settings_obj.clean_join))
                continue

        # A globally muted user is restricted on entry only where the owner is
        # present and the bot has admin permission to restrict members.
        global_mute_reason = await global_mute_service.is_muted(session, user.id)
        if global_mute_reason is not None:
            enforced = await global_mute_service.enforce(
                bot, session, chat_id=message.chat.id, user_id=user.id,
                user_name=user.full_name or "", reason=global_mute_reason,
                notify=True)
            if enforced:
                continue

        # Federation enforcement
        if settings_obj.federation_id and settings_obj.fed_enforce:
            from ..services import federation as fed_service

            record = await fed_service.is_fbanned(session, int(settings_obj.federation_id), user.id)
            if record is not None:
                await safe_restrict(bot, message.chat.id, user.id, MUTE_PERMISSIONS,
                                    context="fedban")
                await safe_send(bot, message.chat.id,
                                f"🌐 این کاربر در فدراسیون بن شده است.\n📌 {record.reason or '—'}",
                                parse_mode="HTML")
                continue

        # Anti-raid forced captcha
        force_captcha = bool(settings_obj.antiraid_lock_new and
                             antiraid.raid_tracker.is_active(message.chat.id) and
                             settings_obj.antiraid_force_captcha)
        if settings_obj.captcha_enabled or force_captcha:
            await captcha.start_captcha(bot, session, chat_id=message.chat.id, user=user,
                                        mode=settings_obj.captcha_mode or "button",
                                        timeout=int(settings_obj.captcha_timeout or 120),
                                        max_attempts=int(settings_obj.captcha_max_attempts or 3),
                                        chat_title=chat.title or "")
            if settings_obj.captcha_delete_after or settings_obj.clean_join:
                await safe_delete(bot, message.chat.id, message.message_id, context="join_cleanup")
            continue

        if settings_obj.clean_join:
            await safe_delete(bot, message.chat.id, message.message_id, context="clean_join")

        await welcome_service.send_welcome(bot, session, chat_id=message.chat.id, user=user,
                                           chat_title=chat.title or "",
                                           member_count=chat.member_count or 0)


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, session: AsyncSession, bot: Bot) -> None:
    """Handle member status transitions, including leave bans and global bans.

    Telegram sends these updates to chat administrators; the leave guard uses
    the old/new status pair to distinguish a voluntary leave from an admin kick.
    """
    old, new = update.old_chat_member, update.new_chat_member
    if old is None or new is None:
        return
    user = new.user
    if user is None or user.id == bot.id:
        return

    # The structured chat_member update is the reliable signal for a voluntary
    # departure. Run the leave guard only for ordinary members; never treat an
    # administrator's departure or an admin-issued kick as an automatic ban.
    left_voluntarily = (update.chat.type in {"group", "supergroup"}
                        and old.status in {"member", "restricted"}
                        and new.status == "left")
    if left_voluntarily:
        leave_settings = await get_settings_cached(session, update.chat.id)
        joined_at = await _joined_at(session, update.chat.id, user.id)
        ban_reason, group_notice = await _leave_guard(
            bot, session, update.chat.id, user, leave_settings, joined_at,
            member_status=old.status, chat_title=update.chat.title or "")
        if group_notice is not None:
            schedule_delete(bot, update.chat.id, group_notice.message_id,
                            GROUP_BOT_MESSAGE_TTL,
                            context="leave_ban_notice_autodelete")
        if leave_settings.get("leave_notify_owner") and ban_reason is None:
            await leave_guard.notify_leave_event(
                bot, session, chat=update.chat, user=user,
                settings=leave_settings, ban_reason=None)

    # Report bans made directly in Telegram by another group administrator,
    # not just bans issued by this bot's command/automation paths.
    manually_banned = (update.chat.type in {"group", "supergroup"}
                       and old.status in {"member", "restricted", "administrator"}
                       and new.status == "kicked")
    if manually_banned:
        moderator = getattr(update, "from_user", None)
        if moderator is not None and int(moderator.id) != int(bot.id):
            try:
                from ..services.ban_reports import report_successful_ban

                await report_successful_ban(
                    bot, session, chat_id=update.chat.id, target_id=user.id,
                    actor_id=moderator.id,
                    reason="دلیل بن در رویداد تلگرام در دسترس نیست.",
                    source="telegram_manual", chat=update.chat,
                    target=user, actor=moderator)
            except Exception as exc:  # noqa: BLE001 - reporting must not block member tracking
                logger.warning("manual-ban report failed (%s)", exc.__class__.__name__)

    # Every status change teaches the bot about a member (somebody who is
    # muted, promoted or restricted is often a member who never speaks).
    await roster_service.remember(
        session, update.chat.id, user,
        status=("left" if new.status in {"left", "kicked"} else new.status),
        left=new.status in {"left", "kicked"})

    # Sticky ban: somebody who is still banned must never slip back in.
    rejoined = old.status in {"left", "kicked"} and new.status in {
        "member", "administrator", "creator", "restricted"}
    if rejoined:
        guard = await get_settings_cached(session, update.chat.id)
        if guard.get("sticky_ban", True) and await mod.enforce_sticky_ban(
                bot, session, update.chat.id, user.id):
            await safe_send(
                bot, update.chat.id,
                f"🔒 <a href=\"tg://user?id={user.id}\">{user.full_name or user.id}</a> "
                f"هنوز بن است و دوباره اخراج شد.\n"
                f"برای بازگشت، یک مدیر باید دستور <code>رفع بن</code> را بزند.",
                parse_mode="HTML")
            return

    if user.id == int(app_settings.owner_id or 0):
        global_mute_service.invalidate_chat(update.chat.id)
    joined = old.status in {"left", "kicked"} and new.status in {
        "member", "administrator", "creator", "restricted"}
    if not joined:
        return
    if app_settings.global_ban_enabled:
        reason = await gban_service.is_banned(session, user.id)
        if reason is not None:
            await gban_service.enforce(bot, session, chat_id=update.chat.id,
                                       chat_title=update.chat.title or "", user_id=user.id,
                                       user_name=user.full_name or "", reason=reason)
            return
    mute_reason = await global_mute_service.is_muted(session, user.id)
    if mute_reason is not None:
        await global_mute_service.enforce(
            bot, session, chat_id=update.chat.id, user_id=user.id,
            user_name=user.full_name or "", reason=mute_reason, notify=True)


@router.message(F.left_chat_member)
async def on_left_member(message: Message, session: AsyncSession, bot: Bot) -> None:
    settings_obj = await get_settings(session, message.chat.id)
    user = message.left_chat_member
    if user is None:
        return
    if user.id == bot.id:
        chat = await session.get(Chat, message.chat.id)
        if chat is not None:
            chat.is_active = False
        await connection_service.drop_chat(session, message.chat.id)
        return
    await welcome_service.record_leave(session, message.chat.id, user.id)
    if settings_obj.clean_leave:
        await safe_delete(bot, message.chat.id, message.message_id, context="clean_leave")
    if settings_obj.goodbye_enabled:
        await welcome_service.send_goodbye(bot, session, chat_id=message.chat.id, user=user,
                                           chat_title=message.chat.title or "")


@router.message(F.pinned_message | F.new_chat_title | F.new_chat_photo |
                F.delete_chat_photo | F.group_chat_created | F.voice_chat_started |
                F.voice_chat_ended | F.message_auto_delete_timer_changed)
async def on_service_message(message: Message, session: AsyncSession, bot: Bot) -> None:
    await welcome_service.maybe_clean_service(bot, session, message, chat_id=message.chat.id,
                                              chat_type=message.chat.type)


@router.chat_join_request()
async def on_join_request(update: ChatJoinRequest, session: AsyncSession,
                          bot: Bot) -> None:
    """Auto-approve «درخواست عضویت» when the group enabled it.

    A private group that requires admin approval sends a ``chat_join_request``
    update for every applicant.  Approving needs the «افزودن کاربران» right.
    Globally banned users are declined instead of waved through.
    """
    settings_obj = await get_settings_cached(session, update.chat.id)

    user = update.from_user
    await get_or_create_user(session, user)

    # Entry path #1: a member who is still banned in THIS group must never get
    # back in, however they try - not even through a join request that an admin
    # (or the auto-approve rule) would otherwise accept.
    if await mod.has_active_ban(session, update.chat.id, user.id):
        try:
            await bot.decline_chat_join_request(chat_id=update.chat.id,
                                                user_id=user.id)
            logger.info("join request declined (active ban) user=%s chat=%s",
                        user.id, update.chat.id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("decline join request failed: %s", exc)
        return

    if not settings_obj.get("auto_approve_join_requests"):
        return

    if app_settings.global_ban_enabled:
        reason = await gban_service.is_banned(session, user.id)
        if reason is not None:
            try:
                await bot.decline_chat_join_request(chat_id=update.chat.id,
                                                    user_id=user.id)
                logger.info("join request declined (global ban) user=%s chat=%s",
                            user.id, update.chat.id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("decline join request failed: %s", exc)
            return

    try:
        await bot.approve_chat_join_request(chat_id=update.chat.id, user_id=user.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not approve join request user=%s chat=%s: %s",
                       user.id, update.chat.id, exc)
        return

    logger.info("join request approved user=%s chat=%s", user.id, update.chat.id)
    if settings_obj.get("log_enabled") and settings_obj.get("log_chat_id"):
        await safe_send(bot, settings_obj["log_chat_id"],
                        f"✅ درخواست عضویت "
                        f"<a href=\"tg://user?id={user.id}\">{user.full_name or user.id}</a> "
                        f"به‌طور خودکار پذیرفته شد.", parse_mode="HTML")


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, session: AsyncSession, bot: Bot) -> None:
    """React to the bot being promoted / demoted / removed from a chat."""
    chat = await get_or_create_chat(session, update.chat)
    new_status = update.new_chat_member.status
    if new_status in {"administrator", "creator"}:
        chat.bot_is_admin = True
        chat.is_active = True
        logger.info("bot promoted in chat %s", update.chat.id)
        from .approval import request_approval

        await request_approval(bot, session, update.chat)
    elif new_status in {"left", "kicked"}:
        chat.bot_is_admin = False
        chat.is_active = False
        logger.info("bot removed from chat %s", update.chat.id)
    elif new_status == "member":
        chat.bot_is_admin = False
    global_mute_service.invalidate_chat(update.chat.id)
    await session.flush()


@router.message_reaction()
async def on_message_reaction(update: MessageReactionUpdated, session: AsyncSession,
                              bot: Bot) -> None:
    """A reaction tells us who is here - even members who never write."""
    user = getattr(update, "user", None)
    if user is None and getattr(update, "actor_chat", None) is not None:
        return  # a channel reacted, not a member we could mention
    if user is None:
        return
    await roster_service.remember(session, update.chat.id, user)
    if app_settings.global_ban_enabled:
        reason = await gban_service.is_banned(session, user.id)
        if reason is not None:
            await gban_service.enforce(bot, session, chat_id=update.chat.id,
                                       chat_title=update.chat.title or "", user_id=user.id,
                                       user_name=user.full_name or "", reason=reason)
            return
    mute_reason = await global_mute_service.is_muted(session, user.id)
    if mute_reason is not None:
        await global_mute_service.enforce(
            bot, session, chat_id=update.chat.id, user_id=user.id,
            user_name=user.full_name or "", reason=mute_reason)


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """CAPTCHA answer: only the target user may answer their own challenge."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        return
    token, index_raw = parts[1], parts[2]
    if not index_raw.isdigit():
        return
    # Pressing the CAPTCHA button proves this member is here: keep them.
    if callback.message is not None:
        await roster_service.remember(session, callback.message.chat.id, callback.from_user)
    solved, message_text = await captcha.solve(bot, session, token=token,
                                               user_id=callback.from_user.id,
                                               choice_index=int(index_raw))
    try:
        await callback.answer(text=message_text, show_alert=not solved)
    except Exception:  # noqa: BLE001
        pass
    if solved and callback.message:
        await safe_delete(bot, callback.message.chat.id, callback.message.message_id,
                          context="captcha_done")
