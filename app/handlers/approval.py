"""Optional «group approval» gate for private deployments.

Armando is a **public** multi-tenant bot by default: anybody may add it to a
group and it simply works.  Some operators prefer the opposite - the bot should
stay silent until the owner has whitelisted the group.

That behaviour is opt-in through ``REQUIRE_GROUP_APPROVAL=true``.  When the flag
is off this module does nothing at all, so the public behaviour is untouched.

Flow
----
1. the bot is added to a group  -> ``request_approval`` is called,
2. the owner receives a private message with ✅ / ❌ buttons,
3. approving sets ``chats.is_approved``; declining makes the bot leave.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import (CallbackQuery, Chat as ChatType,
                           InlineKeyboardButton, InlineKeyboardMarkup)
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_settings
from ..core.errors import safe_send
from .common import CommandContext
from .registry import command

router = Router(name="approval")
logger = logging.getLogger("armando.approval")

CALLBACK_PREFIX = "gapr:"

# Throttles so an unapproved group never spams the group chat or the owner.
_NOTICE_TTL = 3600.0        # remind the group at most once an hour
_REQUEST_TTL = 6 * 3600.0   # re-ask the owner at most every six hours
_notice_at: dict[int, float] = {}
_request_at: dict[int, float] = {}


def _throttled(store: dict[int, float], key: int, ttl: float) -> bool:
    """True when the action may run now; records the attempt when it may."""
    import time

    now = time.monotonic()
    last = store.get(key)
    if last is not None and now - last < ttl:
        return False
    store[key] = now
    return True


def forget(chat_id: int) -> None:
    """Drop the throttles for a chat (used after approval / in tests)."""
    _notice_at.pop(chat_id, None)
    _request_at.pop(chat_id, None)


def approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید گروه",
                             callback_data=f"{CALLBACK_PREFIX}{chat_id}:yes"),
        InlineKeyboardButton(text="❌ رد و خروج",
                             callback_data=f"{CALLBACK_PREFIX}{chat_id}:no"),
    ]])


async def request_approval(bot: Bot, session: AsyncSession, chat: ChatType,
                           *, force: bool = False) -> bool:
    """Ask the bot owner to whitelist ``chat``.  Returns False when disabled."""
    if not app_settings.require_group_approval:
        return False
    if not app_settings.owner_id:
        logger.warning("REQUIRE_GROUP_APPROVAL is on but OWNER_ID is not set")
        return False

    from ..db.models import Chat

    row = await session.get(Chat, chat.id)
    if row is None:  # the chat row may not exist yet on the very first update
        row = Chat(id=chat.id, title=chat.title or "", type=chat.type or "")
        session.add(row)
        try:
            await session.flush()
        except Exception:  # noqa: BLE001 - another update may have created it
            await session.rollback()
            row = await session.get(Chat, chat.id)
            if row is None:
                return False
    if row.is_approved:
        return False

    if not force and not _throttled(_request_at, chat.id, _REQUEST_TTL):
        return False

    sent = await safe_send(
        bot, app_settings.owner_id,
        f"🔔 <b>درخواست فعال‌سازی</b>\n\n"
        f"گروه: {chat.title or '(بدون عنوان)'}\n"
        f"شناسه: <code>{chat.id}</code>\n"
        f"نوع: {chat.type}",
        parse_mode="HTML", reply_markup=approval_keyboard(chat.id))
    if sent is None:
        _request_at.pop(chat.id, None)  # allow a retry on the next message
        logger.warning("could not deliver the approval request for chat=%s",
                       chat.id)
        return False
    logger.info("approval requested for chat=%s", chat.id)
    return True


async def maybe_notify_unapproved(bot: Bot, session: AsyncSession, chat: ChatType) -> None:
    """Owner request + a single, throttled notice inside the group.

    Called from the gate for every update of a group that is not whitelisted.
    Both messages are rate limited so the group is never flooded.
    """
    await request_approval(bot, session, chat)
    if not _throttled(_notice_at, chat.id, _NOTICE_TTL):
        return
    await safe_send(bot, chat.id,
                    "⏳ <b>ربات هنوز فعال نشده است.</b>\n\n"
                    "درخواست فعال‌سازی برای مالک ربات ارسال شد؛ "
                    "پس از تایید، ربات در این گروه کار خواهد کرد.",
                    parse_mode="HTML")


async def _resolve(bot: Bot, session: AsyncSession, chat_id: int, approve: bool,
                   actor_id: int) -> str:
    from ..db.models import Chat

    row = await session.get(Chat, chat_id)
    if row is None:
        return "❌ گروه در پایگاه داده پیدا نشد."
    if approve:
        row.is_approved = True
        await session.commit()   # commit: the gate reads with another session
        await safe_send(bot, chat_id,
                        "✅ ربات در این گروه فعال شد.\n"
                        "برای شروع دستور <code>پنل</code> را بزنید.",
                        parse_mode="HTML")
        return f"✅ گروه <code>{chat_id}</code> تایید شد."
    await safe_send(bot, chat_id,
                    "❌ استفاده از این ربات برای این گروه تایید نشد.")
    try:
        await bot.leave_chat(chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("leave_chat failed chat=%s: %s", chat_id, exc)
    return f"❌ درخواست گروه <code>{chat_id}</code> رد شد و ربات خارج شد."


@router.callback_query(F.data.startswith(CALLBACK_PREFIX))
async def on_approval_callback(callback: CallbackQuery, session: AsyncSession,
                               bot: Bot) -> None:
    payload = (callback.data or "")[len(CALLBACK_PREFIX):]
    if ":" not in payload:
        await callback.answer()
        return
    raw_id, _, choice = payload.partition(":")
    try:
        chat_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return

    if callback.from_user.id != app_settings.owner_id:
        await callback.answer("⛔️ فقط مالک ربات می‌تواند این را تایید کند.",
                              show_alert=True)
        return

    text = await _resolve(bot, session, chat_id, choice == "yes",
                          callback.from_user.id)
    await callback.answer("انجام شد")
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Owner commands (work regardless of the flag, so the owner stays in control)
# --------------------------------------------------------------------------- #
def _is_owner(user_id: int) -> bool:
    return bool(app_settings.owner_id) and user_id == app_settings.owner_id


async def _may_manage_approval(ctx: CommandContext) -> bool:
    if ctx.message.chat.type not in {"group", "supergroup"}:
        return False
    from ..services import owner_access

    return await owner_access.can_use_root_permission(
        ctx.bot, ctx.session, chat_id=ctx.chat_id, user_id=ctx.user_id,
        permission="approval_manage")


async def cmd_approve_group(bot: Bot, session: AsyncSession, chat_id: int,
                            target: int, approve: bool) -> str:
    if not _is_owner(chat_id):
        return "⛔️ این دستور فقط برای مالک ربات است."
    return await _resolve(bot, session, target, approve, chat_id)


# --------------------------------------------------------------------------- #
def _owner_only_message() -> str:
    """Why nothing happened - silent returns leave the admin confused."""
    if not app_settings.owner_id:
        return ("⛔️ این دستور فقط برای مالک ربات است، اما متغیر "
                "<code>OWNER_ID</code> تنظیم نشده است.\n"
                "مقدار آن را برابر شناسه عددی خودتان قرار دهید.")
    return "⛔️ این دستور فقط برای مالک ربات است (ادمین‌ها و سازندهٔ گروه نه)."


@command("تایید گروه", "فعال کردن گروه", role="founder", category="owner",
         description="تایید دسترسی یک گروه (مالک ربات)",
         usage="تایید گروه -1001234567890", allow_in_private=True)
async def cmd_approve(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    if not is_owner and not await _may_manage_approval(ctx):
        await ctx.reply(_owner_only_message())
        return
    if is_owner:
        target = _parse_chat_id(ctx.arg_text)
        if target is None:
            await ctx.reply("⚠️ قالب: <code>تایید گروه -1001234567890</code>")
            return
    else:
        target = ctx.chat_id
        if ctx.arg_text and _parse_chat_id(ctx.arg_text) != target:
            await ctx.reply("⛔️ اختیار واگذارشده فقط اجازهٔ تایید همین گروه را می‌دهد.")
            return
    await ctx.reply(await _resolve(ctx.bot, ctx.session, target, True, ctx.user_id))


@command("رد گروه", "مسدود کردن گروه", role="founder", category="owner",
         description="رد درخواست یک گروه و خروج ربات",
         usage="رد گروه -1001234567890", allow_in_private=True)
async def cmd_reject(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    if not is_owner and not await _may_manage_approval(ctx):
        await ctx.reply(_owner_only_message())
        return
    if is_owner:
        target = _parse_chat_id(ctx.arg_text)
        if target is None:
            await ctx.reply("⚠️ قالب: <code>رد گروه -1001234567890</code>")
            return
    else:
        target = ctx.chat_id
        if ctx.arg_text and _parse_chat_id(ctx.arg_text) != target:
            await ctx.reply("⛔️ اختیار واگذارشده فقط اجازهٔ رد و خروج ربات از همین گروه را می‌دهد.")
            return
    await ctx.reply(await _resolve(ctx.bot, ctx.session, target, False, ctx.user_id))


@command("وضعیت تایید", role="founder", category="owner",
         description="نمایش وضعیت دروازهٔ تایید گروه‌ها", allow_in_private=True)
async def cmd_approval_status(ctx: CommandContext) -> None:
    if not ctx.actor.is_bot_owner and not await _may_manage_approval(ctx):
        await ctx.reply(_owner_only_message())
        return
    state = "🟢 روشن" if app_settings.require_group_approval else "🔴 خاموش"
    await ctx.reply(
        f"🔐 <b>دروازهٔ تایید گروه</b>: {state}\n\n"
        f"در حالت خاموش، ربات عمومی است و در هر گروهی که اضافه شود کار می‌کند.\n"
        f"برای روشن کردن: <code>REQUIRE_GROUP_APPROVAL=true</code>",
        parse_mode="HTML")


def _parse_chat_id(text: str) -> int | None:
    """Accept ``-1001234567890`` in Persian or ASCII digits."""
    from ..core.normalization import normalize_digits

    raw = normalize_digits((text or "").strip(), to="ascii")
    raw = raw.replace(" ", "")
    try:
        return int(raw)
    except ValueError:
        return None
