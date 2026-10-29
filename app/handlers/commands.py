"""Slash commands and the Persian text-command dispatcher."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_send
from ..core.ratelimit import command_limiter
from ..db.models import Chat
from ..keyboards.menus import BOT_NAME
from ..services import connection as connection_service
from ..services.chat_state import get_or_create_chat, get_or_create_user, get_settings_cached
from ..services.permissions import build_actor
from .common import CommandContext
from .panel import send_panel
from .registry import registry

router = Router(name="commands")
logger = logging.getLogger("armando.commands")

WELCOME_PRIVATE = (
    f"🤖 <b>{BOT_NAME}</b>\n\n"
    "سلام! من یک ربات مدیریت گروه فارسی هستم.\n\n"
    "🔹 مدیریت و نظارت کاربران\n"
    "🔹 قفل‌های پیشرفته و ضداسپم\n"
    "🔹 کپچا، ضد Raid و حالت شب\n"
    "🔹 فیلتر، یادداشت و دستورات شخصی\n"
    "🔹 آمار، لاگ و پشتیبان‌گیری\n"
    "🔹 سرگرمی، فال حافظ، ارز و طلا\n\n"
    "برای استفاده، من را به گروه خود اضافه و ادمین کنید، سپس دستور <code>پنل</code> را بزنید.\n\n"
    "📚 <code>راهنما</code> — نمایش راهنمای کامل"
)


async def bot_username(bot: Bot) -> str:
    """The bot's @username (cached for the whole process)."""
    cached = getattr(bot, "_armando_username", None)
    if cached:
        return str(cached)
    username = ""
    try:
        me = await bot.get_me()
        username = me.username or ""
        setattr(bot, "_armando_username", username)
    except Exception:  # noqa: BLE001 - /start must never fail because of this
        username = ""
    return username


async def start_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    """/start buttons: creator credit + "add me to your group"."""
    from ..config import settings

    username = await bot_username(bot)
    rows: list[list[InlineKeyboardButton]] = []
    if username:
        rows.append([InlineKeyboardButton(
            text="➕ اضافه کردن به گروه",
            url=f"https://t.me/{username}?startgroup=true")])
    rows.append([InlineKeyboardButton(
        text=f"👤 سازنده بات : {settings.creator_label}",
        url=settings.creator_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class IsPersianCommand:
    """aiogram filter: only pass when the message really is a registered command."""

    def __init__(self) -> None:
        pass

    def __call__(self, message: Message) -> bool | dict:
        """Synchronous on purpose: aiogram awaits coroutine functions only."""

        raw = message.text or message.caption or ""
        if not raw or raw.startswith("/"):
            return False
        match = registry.match(raw, has_reply=message.reply_to_message is not None)
        if match is None:
            return False
        return {"command_match": match}


# --------------------------------------------------------------------------- #
# Slash commands (kept to the minimum; all answers are Persian)
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def on_start(message: Message, session: AsyncSession, bot: Bot) -> None:
    await get_or_create_user(session, message.from_user)
    if message.chat.type == "private":
        await safe_send(bot, message.chat.id, WELCOME_PRIVATE, parse_mode=ParseMode.HTML,
                        reply_markup=await start_keyboard(bot))
        from ..services.ban_reports import deliver_pending_reports

        await deliver_pending_reports(bot, session, message.from_user.id)
        return
    await get_or_create_chat(session, message.chat)
    await safe_send(
        bot, message.chat.id,
        "🤖 ربات آماده است.\nبرای نمایش پنل مدیریتی: <code>پنل</code>\n"
        "برای راهنما: <code>راهنما</code>", parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def on_slash_help(message: Message, session: AsyncSession, bot: Bot) -> None:
    await safe_send(bot, message.chat.id,
                    "📚 برای مشاهده راهنما بنویسید: <code>راهنما</code>\n"
                    "🎛 برای پنل مدیریتی: <code>پنل</code>", parse_mode=ParseMode.HTML)


@router.message(Command("id"))
async def on_slash_id(message: Message, session: AsyncSession, bot: Bot) -> None:
    target_id = message.from_user.id
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    await safe_send(bot, message.chat.id,
                    f"🔢 شناسه کاربر: <code>{target_id}</code>\n"
                    f"💬 شناسه چت: <code>{message.chat.id}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("panel"))
async def on_slash_panel(message: Message, session: AsyncSession, bot: Bot) -> None:
    await get_or_create_user(session, message.from_user)
    if message.chat.type != "private":
        await get_or_create_chat(session, message.chat)
    await send_panel(bot, message.chat.id, message.from_user.id, session, message=message)


@router.message(Command("connect"))
async def on_slash_connect(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.chat.type != "private":
        await safe_send(bot, message.chat.id,
                        "🔗 این دستور فقط در چت خصوصی ربات کار می‌کند.", parse_mode=ParseMode.HTML)
        return
    chats = await connection_service.available_chats(bot, session, message.from_user.id)
    if not chats:
        await safe_send(bot, message.chat.id,
                        "ℹ️ شما در هیچ گروهی مدیر نیستید. ابتدا در گروه دستور <code>پنل</code> را بزنید.",
                        parse_mode=ParseMode.HTML)
        return
    from ..keyboards.factory import cb, markup, primary, row

    buttons = [primary(title, cb("conn", chat_id)) for chat_id, title in chats]
    await safe_send(bot, message.chat.id, "🔗 یکی از گروه‌ها را انتخاب کنید:",
                    reply_markup=markup([*row(*buttons)]), parse_mode=ParseMode.HTML)


@router.message(Command("settings"))
async def on_slash_settings(message: Message, session: AsyncSession, bot: Bot) -> None:
    await on_slash_panel(message, session, bot)


# --------------------------------------------------------------------------- #
# Persian text command dispatcher
# --------------------------------------------------------------------------- #
@router.message(IsPersianCommand())
async def on_text_command(message: Message, session: AsyncSession, bot: Bot,
                          command_match) -> None:
    match = command_match
    command = match.command
    user = message.from_user
    if user is None or user.is_bot:
        return

    chat_type = message.chat.type
    if command.group_only and chat_type == "private":
        return
    if command.private_only and chat_type != "private":
        return
    if chat_type == "private" and not (command.allow_in_private or command.private_only):
        return

    # rate limit
    allowed, retry = command_limiter.check((message.chat.id, user.id))
    if not allowed:
        await safe_send(bot, message.chat.id,
                        f"⏳ تعداد درخواست‌های شما زیاد است. {int(retry)} ثانیه صبر کنید.",
                        parse_mode=ParseMode.HTML)
        return

    await get_or_create_user(session, user)
    if chat_type != "private":
        await get_or_create_chat(session, message.chat)
        if chat_type in {"group", "supergroup"}:
            from ..services import global_mute as global_mute_service

            mute_reason = await global_mute_service.is_muted(session, user.id)
            if mute_reason is not None:
                enforced = await global_mute_service.enforce(
                    bot, session, chat_id=message.chat.id, user_id=user.id,
                    user_name=user.full_name or "", reason=mute_reason,
                    message_id=message.message_id)
                if enforced:
                    await session.commit()
                    return

    actor = await build_actor(bot, message.chat.id, user.id, session=session,
                              chat_type=chat_type, from_user=user)
    settings = await get_settings_cached(session, message.chat.id)
    chat = await session.get(Chat, message.chat.id)

    ctx = CommandContext(
        bot=bot,
        message=message,
        session=session,
        actor=actor,
        command=match.phrase,
        args=list(match.args),
        raw_text=message.text or message.caption or "",
        settings=settings,
        chat_title=(chat.title if chat else "") or (message.chat.title or ""),
    )
    try:
        await command.handler(ctx)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - a failing command must not kill the bot
        logger.exception("command failed: %s (%s)", match.phrase, exc)
        await session.rollback()
        await safe_send(bot, message.chat.id,
                        "⚠️ خطایی هنگام اجرای دستور رخ داد. لطفاً دوباره تلاش کنید.",
                        parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Info command available in private
# --------------------------------------------------------------------------- #
@router.message(F.chat.type == "private", F.text)
async def on_private_text(message: Message, session: AsyncSession, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return
    from ..services.instagram import handle_instagram_message

    if await handle_instagram_message(message, bot):
        return
    from ..services.ai_chat import handle_ai_message

    if await handle_ai_message(message, bot):
        return
    connected = await connection_service.get_active(session, message.from_user.id)
    if connected:
        await safe_send(
            bot, message.chat.id,
            "🔗 شما به یک گروه متصل هستید.\n"
            "برای مدیریت از همین‌جا روی دکمه زیر بزنید و از پنل استفاده کنید.",
            parse_mode=ParseMode.HTML)
        await send_panel(bot, message.chat.id, message.from_user.id, session, message=message)
        return
    await safe_send(
        bot, message.chat.id,
        "🤖 برای مدیریت گروه‌ها از پنل استفاده کنید.\n"
        "اگر مدیر یک گروه هستید، ابتدا در گروه دستور <code>پنل</code> را بزنید و سپس اینجا "
        "<code>اتصال</code> را اجرا کنید.\n\n"
        "📚 <code>راهنما</code> — مشاهده راهنما",
        parse_mode=ParseMode.HTML)
