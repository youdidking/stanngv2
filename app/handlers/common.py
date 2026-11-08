"""Shared handler helpers: command context, permission guards, target parsing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_delete, safe_send
from ..core.message_lifecycle import GROUP_BOT_MESSAGE_TTL
from ..core.normalization import normalize_text
from ..db.models import User
from ..keyboards.factory import InlineKeyboardMarkup, cb, markup, primary, row
from ..services.permissions import Actor, check_moderation_target, check_target
from ..services.targeting import TargetResult, extract_duration_and_reason, resolve_target

logger = logging.getLogger("armando.handlers")


@dataclass
class CommandContext:
    """Everything a command handler needs, resolved once per update."""

    bot: Bot
    message: Message
    session: AsyncSession
    actor: Actor
    command: str = ""
    args: list[str] = field(default_factory=list)
    raw_text: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    chat_title: str = ""

    # ---------------------------------------------------------------- helpers
    @property
    def chat_id(self) -> int:
        return self.message.chat.id

    @property
    def user_id(self) -> int:
        return self.message.from_user.id if self.message.from_user else 0

    @property
    def is_group(self) -> bool:
        return self.message.chat.type in {"group", "supergroup"}

    @property
    def arg_text(self) -> str:
        return " ".join(self.args).strip()

    async def reply(self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None,
                    parse_mode: str = ParseMode.HTML, disable_preview: bool = True,
                    reply_to: bool = True, delete_after: int = 0):
        kwargs: dict[str, Any] = {
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if reply_to:
            kwargs["reply_to_message_id"] = self.message.message_id
        message = await safe_send(self.bot, self.chat_id, text, **kwargs)
        if self.is_group and delete_after > 0:
            delete_after = GROUP_BOT_MESSAGE_TTL
        if message is not None and delete_after > 0:
            import asyncio

            async def _later() -> None:
                await asyncio.sleep(delete_after)
                await safe_delete(self.bot, self.chat_id, message.message_id, context="auto_delete")

            asyncio.create_task(_later())
        return message

    async def delete_invocation(self) -> None:
        await safe_delete(self.bot, self.chat_id, self.message.message_id, context="delete_command")

    async def target(self) -> TargetResult:
        return await resolve_target(self.bot, self.session, self.message, self.args)

    def duration_and_reason(self, tokens: list[str] | None = None) -> tuple[int | None, str]:
        duration, reason, _ = extract_duration_and_reason(tokens if tokens is not None else self.args)
        return duration, reason


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
ROLE_MESSAGES = {
    "admin": "⛔️ این دستور فقط برای مدیران گروه است.",
    "moderator": "⛔️ این دستور فقط برای ناظران و بالاتر است.",
    "helper": "⛔️ این دستور فقط برای کمک‌یاران و بالاتر است.",
    "cleaner": "⛔️ این دستور فقط برای پاکسازها و بالاتر است.",
    "muter": "⛔️ این دستور فقط برای ساکت‌کننده‌ها و بالاتر است.",
    "trusted": "⛔️ این دستور مخصوص کاربران ویژه است.",
    "member": "",
}

PERMISSION_MESSAGES = {
    "ban": "⛔️ شما اجازه «بن کردن» در این گروه را ندارید (دسترسی تلگرام شما ناکافی است).",
    "kick": "⛔️ شما اجازه «اخراج کردن» در این گروه را ندارید.",
    "restrict": "⛔️ شما اجازه «محدود کردن اعضا» در این گروه را ندارید.",
    "delete": "⛔️ شما اجازه «حذف پیام» در این گروه را ندارید.",
    "promote": "⛔️ شما اجازه «ارتقای مدیران» در این گروه را ندارید.",
    "pin": "⛔️ شما اجازه «پین کردن پیام» در این گروه را ندارید.",
    "change_info": "⛔️ شما اجازه «تغییر اطلاعات گروه» را ندارید.",
    "manage": "⛔️ فقط مدیران گروه می‌توانند تنظیمات را تغییر دهند.",
}

PERMISSION_CHECKERS = {
    "ban": lambda actor: actor.can_ban(),
    "kick": lambda actor: actor.can_kick(),
    "restrict": lambda actor: actor.can_restrict(),
    "delete": lambda actor: actor.can_delete(),
    "promote": lambda actor: actor.can_promote(),
    "pin": lambda actor: actor.can_pin(),
    "change_info": lambda actor: actor.can_edit_chat_info(),
    "manage": lambda actor: actor.can_manage_settings(),
    "filters": lambda actor: actor.can_manage_filters(),
    "notes": lambda actor: actor.can_manage_notes(),
    "staff": lambda actor: actor.can_manage_staff(),
    "captcha": lambda actor: actor.can_manage_captcha(),
    "purge": lambda actor: actor.can_use_purge(),
    "invite": lambda actor: actor.can_invite_via_link(),
}


def _status_hint(actor) -> str:
    """Small diagnostic line: helps admins see what the bot detected."""
    if actor is None:
        return ""
    if actor.is_chat_creator:
        status = "سازنده گروه"
    elif actor.anonymous_admin:
        status = "مدیر ناشناس"
    elif actor.telegram_status == "administrator":
        status = "مدیر تلگرام"
    elif actor.telegram_status == "member":
        status = "عضو عادی"
    else:
        status = actor.telegram_status or "نامشخص"
    role = actor.bot_role
    hint = f"\n🔎 وضعیت شناسایی‌شده: {status}"
    if role:
        hint += f" • نقش داخلی: {role}"
    hint += ("\nℹ️ اگر مدیر گروه هستید، چند ثانیه بعد دوباره تلاش کنید "
             "(ربات وضعیت را تازه می‌خواند) و بررسی کنید ربات هم ادمین باشد.")
    return hint


async def require(ctx: CommandContext, *, role: str | None = None,
                  permission: str | None = None, quiet: bool = False) -> bool:
    """Centralized authorization gate for command handlers."""
    if role and not ctx.actor.has_role(role):
        if not quiet:
            await ctx.reply(ROLE_MESSAGES.get(role, "⛔️ شما اجازه انجام این کار را ندارید."))
        return False
    if permission:
        checker = PERMISSION_CHECKERS.get(permission)
        if checker is not None and not checker(ctx.actor):
            # The cached Telegram status may be stale (the user was promoted a
            # moment ago): drop it so the next attempt re-reads it from Telegram.
            from ..core import cache

            cache.invalidate_user(ctx.chat_id, ctx.user_id)
            if not quiet:
                message = PERMISSION_MESSAGES.get(
                    permission, "⛔️ شما اجازه انجام این کار را ندارید."
                    + ("\nℹ️ بررسی کنید که ربات هم دسترسی لازم را داشته باشد." if permission in
                       {"ban", "restrict", "delete", "pin"} else ""))
                await ctx.reply(message + _status_hint(ctx.actor))
            return False
    return True


# --------------------------------------------------------------------------- #
# Undo buttons ("glass" buttons that reverse the action they belong to)
# --------------------------------------------------------------------------- #
UNDO_LABELS: dict[str, str] = {
    "unmute": "↩️ لغو سکوت",
    "unban": "↩️ رفع بن",
    "unwarn": "↩️ کسر اخطار",
    "demote": "↩️ عزل مدیر",
    "deltag": "🗑 حذف تگ",
}


def undo_keyboard(action: str, user_id: int, label: str | None = None) -> InlineKeyboardMarkup:
    """A single glass button that reverses the moderation action just taken."""
    return markup([row(primary(label or UNDO_LABELS.get(action, "↩️ لغو"),
                               cb("undo", action, user_id)))])


async def ensure_bot_admin(ctx: CommandContext, *, permission: str = "restrict") -> bool:
    """Verify the bot itself has the required capability."""
    from ..services.permissions import get_bot_actor

    bot_actor = await get_bot_actor(ctx.bot, ctx.chat_id)
    checker = PERMISSION_CHECKERS.get(permission)
    if checker is None or checker(bot_actor):
        return True
    await ctx.reply(
        "⚠️ ربات برای انجام این کار دسترسی لازم را ندارد.\n"
        "لطفاً ربات را ادمین کنید و دسترسی‌های مورد نیاز (محدودسازی/حذف پیام) را بدهید."
    )
    return False


async def guard_target(ctx: CommandContext, target: TargetResult,
                       *, allow_admin: bool = False) -> bool:
    """Report target resolution / protection problems in Persian."""
    if target.error:
        await ctx.reply(target.error)
        return False
    if not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return False
    reason = await check_target(ctx.bot, ctx.chat_id, target.user_id, ctx.actor,
                                allow_admin=allow_admin)
    if reason:
        await ctx.reply(reason)
        return False
    return True


async def guard_moderation_target(ctx: CommandContext, target: TargetResult, *,
                                 allow_absent: bool = False) -> bool:
    """Validate a moderation target using the group-owner hierarchy."""
    if target.error:
        await ctx.reply(target.error)
        return False
    if not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return False
    reason = await check_moderation_target(
        ctx.bot, ctx.session, ctx.chat_id, target.user_id, ctx.actor,
        allow_absent=allow_absent,
    )
    if reason:
        await ctx.reply(reason)
        return False
    return True


async def display_name(session: AsyncSession, user_id: int) -> str:
    user = await session.get(User, user_id)
    if user is None:
        return str(user_id)
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or f"@{user.username or user_id}"


def user_html(user_id: int | None, name: str) -> str:
    if not user_id:
        return name or "—"
    safe = (name or "کاربر").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def normalize_arg(text: str) -> str:
    return normalize_text(text, mode="command")
