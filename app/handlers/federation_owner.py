"""Federations and owner-level commands (global ban is opt-in)."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from sqlalchemy import func, select

from ..config import settings
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_datetime
from ..db.models import BanRecord, Chat, GlobalMute, ModerationAction, User
from ..services import federation as fed_service
from ..services import gban as gban_service
from ..services import global_mute as global_mute_service
from ..services import owner_access
from ..services.chat_state import invalidate_settings
from .common import CommandContext, require, user_html
from .registry import command

logger = logging.getLogger("armando.handlers.federation")


async def _current_fed(ctx: CommandContext):
    from ..services.chat_state import get_settings

    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    if not settings_obj.federation_id:
        return None, settings_obj
    fed = await fed_service.get_fed_by_id(ctx.session, int(settings_obj.federation_id))
    return fed, settings_obj


async def _may_use_owner_permission(ctx: CommandContext, permission: str) -> bool:
    """Owner bypass; delegated capabilities stay limited to their granted chat."""
    return await owner_access.can_use_root_permission(
        ctx.bot, ctx.session, chat_id=ctx.chat_id, user_id=ctx.user_id,
        permission=permission)


# --------------------------------------------------------------------------- #
# Federations
# --------------------------------------------------------------------------- #
@command("ساخت فدراسیون", "ایجاد فدراسیون", role="admin", permission="manage",
         category="federation", description="ساخت فدراسیون جدید")
async def cmd_fed_create(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    name = (ctx.arg_text or "").strip()
    if not name:
        await ctx.reply("⚠️ نام فدراسیون را بنویسید: <code>ساخت فدراسیون myfed</code>")
        return
    try:
        fed = await fed_service.create_fed(ctx.session, name, ctx.user_id)
    except ValueError as exc:
        await ctx.reply(str(exc))
        return
    await ctx.reply(f"🌐 فدراسیون «<code>{fed.name}</code>» ساخته شد.\n"
                    "برای اتصال گروه‌ها: <code>اتحاد فدراسیون نام</code>")


@command("اتحاد فدراسیون", "اتصال فدراسیون", "عضویت فدراسیون", role="admin",
         permission="manage", category="federation", description="اتحاد گروه به فدراسیون")
async def cmd_fed_join(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    name = (ctx.arg_text or "").strip().lower()
    fed = await fed_service.get_fed(ctx.session, name)
    if fed is None:
        await ctx.reply("❌ فدراسیونی با این نام پیدا نشد.")
        return
    joined = await fed_service.join_fed(ctx.session, fed, ctx.chat_id, joined_by=ctx.user_id)
    await ctx.reply(f"🔗 گروه به فدراسیون «<code>{fed.name}</code>» پیوست." if joined
                    else "ℹ️ این گروه از قبل عضو فدراسیون است.")


@command("ترک فدراسیون", "خروج از فدراسیون", role="admin", permission="manage",
         category="federation", description="خروج گروه از فدراسیون")
async def cmd_fed_leave(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    left = await fed_service.leave_fed(ctx.session, ctx.chat_id)
    await ctx.reply("🚪 گروه از فدراسیون خارج شد." if left else "ℹ️ این گروه عضو فدراسیونی نیست.")


@command("اطلاعات فدراسیون", role="member", category="federation",
         description="نمایش اطلاعات فدراسیون")
async def cmd_fed_info(ctx: CommandContext) -> None:
    fed, settings_obj = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    admins = await fed_service.fed_admins(ctx.session, fed)
    chats = await fed_service.fed_chats(ctx.session, fed)
    bans = await fed_service.fban_list(ctx.session, fed)
    await ctx.reply(fed_service.fed_info_text(fed, chats, admins, len(bans)))


@command("مدیران فدراسیون", role="admin", category="federation",
         description="نمایش مدیران فدراسیون")
async def cmd_fed_admins(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    admins = await fed_service.fed_admins(ctx.session, fed)
    lines = ["👮 <b>مدیران فدراسیون</b>", ""]
    for user_id in admins:
        user = await ctx.session.get(User, user_id)
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else str(user_id)
        lines.append(f"• {user_html(user_id, name or str(user_id))}")
    await ctx.reply("\n".join(lines))


@command("افزودن مدیر فدراسیون", role="admin", permission="manage",
         category="federation", description="افزودن مدیر به فدراسیون")
async def cmd_fed_admin_add(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if not await fed_service.is_fed_admin(ctx.session, fed, ctx.user_id):
        await ctx.reply("⛔️ فقط مدیران فدراسیون می‌توانند مدیر اضافه کنند.")
        return
    target = await ctx.target()
    if target is None or not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    await fed_service.add_fed_admin(ctx.session, fed, target.user_id, added_by=ctx.user_id)
    await ctx.reply(f"✅ {user_html(target.user_id, target.full_name)} مدیر فدراسیون شد.")


@command("حذف مدیر فدراسیون", role="admin", permission="manage",
         category="federation", description="حذف مدیر فدراسیون")
async def cmd_fed_admin_remove(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if fed.owner_id != ctx.user_id:
        await ctx.reply("⛔️ فقط مالک فدراسیون می‌تواند مدیران را حذف کند.")
        return
    target = await ctx.target()
    if target is None or not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    removed = await fed_service.remove_fed_admin(ctx.session, fed, target.user_id)
    await ctx.reply("🗑 مدیر فدراسیون حذف شد." if removed else "ℹ️ تغییری ایجاد نشد.")


@command("بن فدراسیون", "افبن", role="admin", permission="manage",
         category="federation", description="بن کردن کاربر در سطح فدراسیون")
async def cmd_fban(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if not await fed_service.is_fed_admin(ctx.session, fed, ctx.user_id):
        await ctx.reply("⛔️ فقط مدیران فدراسیون می‌توانند بن کنند.")
        return
    target = await ctx.target()
    if target is None or not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    _, _, reason = ctx.duration_and_reason(target.rest)
    applied = await fed_service.fban(ctx.session, ctx.bot, fed, user_id=target.user_id,
                                     reason=reason or "بدون دلیل", banned_by=ctx.user_id,
                                     enforce=(fed.ban_mode == "enforce"))
    await ctx.reply(f"🌐 کاربر {user_html(target.user_id, target.full_name)} در فدراسیون بن شد.\n"
                    f"📌 دلیل: {reason or '—'}\n"
                    f"⚡️ اعمال‌شده در {to_persian_digits(str(applied))} گروه")


@command("رفع بن فدراسیون", "آنفبن", role="admin", permission="manage",
         category="federation", description="رفع بن فدراسیون")
async def cmd_funban(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if not await fed_service.is_fed_admin(ctx.session, fed, ctx.user_id):
        await ctx.reply("⛔️ فقط مدیران فدراسیون می‌توانند رفع بن کنند.")
        return
    target = await ctx.target()
    if target is None or not target.user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    removed = await fed_service.funban(ctx.session, ctx.bot, fed, user_id=target.user_id,
                                       unbanned_by=ctx.user_id)
    await ctx.reply("🕊 بن فدراسیون برداشته شد." if removed else "ℹ️ این کاربر در فدراسیون بن نبود.")


@command("لیست بن فدراسیون", role="admin", category="federation",
         description="نمایش لیست بن‌های فدراسیون")
async def cmd_fban_list(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    records = await fed_service.fban_list(ctx.session, fed)
    await ctx.reply(fed_service.fban_list_text(records))


@command("تنظیم قوانین فدراسیون", role="admin", permission="manage",
         category="federation", description="تنظیم قوانین فدراسیون")
async def cmd_fed_rules(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if not await fed_service.is_fed_admin(ctx.session, fed, ctx.user_id):
        await ctx.reply("⛔️ فقط مدیران فدراسیون می‌توانند قوانین را تنظیم کنند.")
        return
    fed.rules = ctx.arg_text.strip()
    await ctx.session.flush()
    await ctx.reply("📜 قوانین فدراسیون ذخیره شد.")


@command("قوانین فدراسیون", role="member", category="federation",
         description="نمایش قوانین فدراسیون")
async def cmd_fed_show_rules(ctx: CommandContext) -> None:
    fed, _ = await _current_fed(ctx)
    if fed is None or not fed.rules:
        await ctx.reply("ℹ️ قوانینی برای فدراسیون تنظیم نشده است.")
        return
    await ctx.reply(f"📜 <b>قوانین فدراسیون {fed.name}</b>\n\n{fed.rules}")


@command("حالت فدراسیون", role="admin", permission="manage", category="federation",
         description="تغییر حالت اجرای بن فدراسیون")
async def cmd_fed_mode(ctx: CommandContext) -> None:
    fed, settings_obj = await _current_fed(ctx)
    if fed is None:
        await ctx.reply("ℹ️ این گروه عضو هیچ فدراسیونی نیست.")
        return
    if not await fed_service.is_fed_admin(ctx.session, fed, ctx.user_id):
        await ctx.reply("⛔️ فقط مدیران فدراسیون می‌توانند این تنظیم را تغییر دهند.")
        return
    mode = "share"
    if ctx.chat_id in await fed_service.fed_chats(ctx.session, fed):
        settings_obj.fed_enforce = not bool(settings_obj.fed_enforce)
        invalidate_settings(ctx.chat_id)
        await ctx.reply("✅ اجرای خودکار بن‌های فدراسیون در این گروه: "
                        + ("🟢 فعال" if settings_obj.fed_enforce else "🔴 غیرفعال"))
        return
    fed.ban_mode = "enforce" if fed.ban_mode == "share" else "share"
    mode = fed.ban_mode
    await ctx.session.flush()
    await ctx.reply("⚙️ حالت فدراسیون: " +
                    ("اجرای خودکار" if mode == "enforce" else "فقط اشتراک‌گذاری"))


# --------------------------------------------------------------------------- #
# Global ban - background rollout
# --------------------------------------------------------------------------- #
async def _rollout(bot, user_id: int, reason: str, source_chat_id: int, *,
                   unban: bool = False) -> None:
    """Apply (or lift) a global ban in every chat the bot administers."""
    from ..db import session_scope

    try:
        async with session_scope() as session:
            if unban:
                count = await gban_service.lift_everywhere(bot, session, user_id,
                                                           skip=source_chat_id)
                text = (f"🕊 بن سراسری در {to_persian_digits(str(count))} گفتگو برداشته شد.")
            else:
                count = await gban_service.enforce_everywhere(bot, session, user_id, reason,
                                                              skip=source_chat_id)
                text = (f"🌍 بن سراسری در {to_persian_digits(str(count))} گفتگوی دیگر اعمال شد."
                        if count else "")
    except Exception:  # noqa: BLE001 - a background job must never crash the bot
        logger.exception("gban rollout failed user=%s", user_id)
        return
    if text:
        from ..core.errors import safe_send

        await safe_send(bot, source_chat_id, text, parse_mode="HTML")


async def _global_mute_rollout(bot, user_id: int, source_chat_id: int, *,
                               snapshot=None) -> None:
    """Apply or lift a global mute after its database state has been committed."""
    from ..db import session_scope
    from ..core.errors import safe_send

    try:
        async with session_scope() as session:
            if snapshot is None:
                chat_ids = await global_mute_service.known_chat_ids(session)
                group_ids = await global_mute_service.known_group_chat_ids(session)
                count = await global_mute_service.apply_everywhere(
                    bot, session, user_id, chat_ids=chat_ids)
                total = len(group_ids)
                if total == 0:
                    text = ("ℹ️ سوپرگروه فعالی در فهرست شناخته‌شدهٔ ربات برای بررسی نیست. "
                            "چت‌ها با دریافت رویداد عضویت، پیام یا به‌روزرسانی تازه وارد فهرست می‌شوند.")
                elif count:
                    text = (f"🔇 سکوت سراسری در {to_persian_digits(str(count))} از "
                            f"{to_persian_digits(str(total))} سوپرگروه ثبت‌شده اعمال شد.\n"
                            "شرط اعمال: ربات مدیرِ دارای حق محدودسازی باشد و مالک بات در سوپرگروه حضور داشته باشد.")
                else:
                    text = (f"ℹ️ از {to_persian_digits(str(total))} سوپرگروه ثبت‌شده، در هیچ‌کدام "
                            "سکوت اعمال نشد. بررسی کنید کاربر عضو و قابل محدودسازی باشد، "
                            "ربات حق محدودسازی اعضا داشته باشد و مالک بات در سوپرگروه حضور داشته باشد. "
                            "سکوت در خود کانال‌ها پشتیبانی نمی‌شود؛ گروه گفت‌وگوی پیوندخورده جداگانه بررسی می‌شود.")
            else:
                count = await global_mute_service.lift_everywhere(
                    bot, session, snapshot)
                record = await global_mute_service.get_record(session, user_id)
                pending = len(record.applied_chat_ids or []) if record is not None else 0
                if record is not None and record.active:
                    text = ("ℹ️ پیش از پایان عملیات، سکوت سراسری دوباره فعال شد؛ "
                            "محدودیت‌های ثبت‌شده حفظ شدند و پاک‌سازی انجام نشد.")
                elif pending:
                    text = (f"⚠️ سکوت سراسری برداشته شد و در {to_persian_digits(str(count))} سوپرگروه "
                            f"پاک‌سازی شد؛ محدودیت {to_persian_digits(str(pending))} سوپرگروه هنوز "
                            "رفع نشده است. دسترسی ربات را بررسی و فرمان رفع سکوت را دوباره اجرا کنید.")
                elif not snapshot.chat_ids and count == 0:
                    text = ("ℹ️ سکوت سراسری غیرفعال شد، اما هیچ محدودیت موفقی برای برداشتن "
                            "در سوپرگروهی ثبت نشده بود.")
                else:
                    text = (f"🔊 محدودیت ثبت‌شدهٔ سکوت سراسری از "
                            f"{to_persian_digits(str(count))} سوپرگروه برداشته شد.")
    except Exception:  # noqa: BLE001 - a background rollout must not crash the bot
        logger.exception("global mute rollout failed user=%s", user_id)
        return
    await safe_send(bot, source_chat_id, text, parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Owner only (global features are OFF unless explicitly enabled)
# --------------------------------------------------------------------------- #
@command("بن سراسری", role="founder", category="owner", description="بن سراسری (مالک ربات)",
         allow_in_private=True)
async def cmd_global_ban(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_ban"):
        await ctx.reply("⛔️ این فرمان فقط برای مالک ربات یا ادمینی که اختیار «بن سراسری» را در همین چت گرفته باشد مجاز است.")
        return
    if not settings.global_ban_enabled:
        await ctx.reply("🔒 قابلیت بن سراسری غیرفعال است.\n"
                        "برای فعال‌سازی متغیر <code>GLOBAL_BAN_ENABLED=true</code> را تنظیم کنید.")
        return
    target = await ctx.target()
    user_id = target.user_id if target else None
    if not user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    _, reason = ctx.duration_and_reason(target.rest if target else [])
    user = await ctx.session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name="")
        ctx.session.add(user)
    user.global_banned = True
    user.global_ban_reason = (reason or "بدون دلیل")[:300]
    user.global_banned_at = datetime.utcnow()
    ctx.session.add(BanRecord(user_id=user_id, banned_by=ctx.user_id,
                              reason=user.global_ban_reason, scope="global", active=True,
                              created_at=datetime.utcnow()))
    await ctx.session.flush()
    gban_service.invalidate(user_id)

    # Ban here and now, then roll it out to every other guarded chat.
    here = await gban_service.ban_in_chat(ctx.bot, ctx.chat_id, user_id)
    await ctx.reply(
        f"🌍 کاربر <code>{user_id}</code> به‌صورت سراسری بن شد.\n"
        f"📌 {user.global_ban_reason}\n"
        + ("✅ در این گفتگو هم بن شد.\n" if here else "")
        + "↻ اعمال در بقیهٔ گروه‌ها و کانال‌ها در پس‌زمینه انجام می‌شود.")

    async def _later() -> None:
        await _rollout(ctx.bot, user_id, user.global_ban_reason or "بدون دلیل", ctx.chat_id)

    asyncio.create_task(_later())


@command("رفع بن سراسری", role="founder", category="owner",
         description="رفع بن سراسری (مالک ربات)", allow_in_private=True)
async def cmd_global_unban(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_ban"):
        await ctx.reply("⛔️ این فرمان فقط برای مالک ربات یا دارندهٔ اختیار «بن سراسری» در همین چت مجاز است.")
        return
    target = await ctx.target()
    user_id = target.user_id if target else None
    from ..core.normalization import normalize_digits

    if not user_id and ctx.arg_text:
        digits = "".join(ch for ch in normalize_digits(ctx.arg_text, to="ascii") if ch.isdigit())
        user_id = int(digits) if digits else None
    if not user_id:
        await ctx.reply("❌ کاربر هدف مشخص نشد.")
        return
    user = await ctx.session.get(User, user_id)
    if user is not None:
        user.global_banned = False
        user.global_ban_reason = None
    await ctx.session.execute(
        select(BanRecord).where(BanRecord.scope == "global", BanRecord.user_id == user_id))
    from sqlalchemy import update

    await ctx.session.execute(
        update(BanRecord).where(BanRecord.scope == "global", BanRecord.user_id == user_id)
        .values(active=False))
    await ctx.session.flush()
    gban_service.invalidate(user_id)
    await ctx.reply(f"🕊 بن سراسری کاربر <code>{user_id}</code> برداشته شد.")

    async def _later() -> None:
        await _rollout(ctx.bot, user_id, "", ctx.chat_id, unban=True)

    asyncio.create_task(_later())


@command("لیست بن سراسری", role="founder", category="owner",
         description="نمایش بن‌های سراسری", allow_in_private=True)
async def cmd_global_ban_list(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_ban"):
        await ctx.reply("⛔️ این فهرست فقط برای مالک ربات یا دارندهٔ اختیار «بن سراسری» در همین چت است.")
        return
    result = await ctx.session.execute(
        select(BanRecord).where(BanRecord.scope == "global", BanRecord.active.is_(True)).limit(50))
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("✅ هیچ بن سراسری فعالی وجود ندارد.")
        return
    lines = ["🌍 <b>بن‌های سراسری</b>", ""]
    for row in rows:
        lines.append(f"• <code>{row.user_id}</code> — 📌 {row.reason or '—'}\n"
                     f"   🕒 {persian_datetime(row.created_at)}")
    await ctx.reply("\n".join(lines))


@command("سکوت سراسری", role="founder", category="owner",
         description="ساکت‌کردن کاربر در سوپرگروه‌های واجد شرایط", allow_in_private=True)
async def cmd_global_mute(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_mute"):
        await ctx.reply("⛔️ این فرمان فقط برای مالک ربات یا دارندهٔ اختیار «سکوت سراسری» در همین چت مجاز است.")
        return
    target = await ctx.target()
    if target is None or target.error or not target.user_id:
        await ctx.reply((target.error if target else None) or "❌ کاربر هدف مشخص نشد.")
        return
    if int(target.user_id) in {
            int(ctx.bot.id), int(ctx.user_id), int(settings.owner_id or 0)}:
        await ctx.reply("⛔️ ربات، خودتان یا مالک بات نمی‌تواند هدف سکوت سراسری باشد.")
        return
    duration, reason = ctx.duration_and_reason(target.rest)
    if duration is not None:
        await ctx.reply("ℹ️ سکوت سراسری دائمی است و مدت نمی‌گیرد؛ برای لغو، «رفع سکوت سراسری» را اجرا کنید.")
        return
    reason = (reason or "بدون دلیل")[:300]
    user = await ctx.session.get(User, target.user_id)
    if user is None:
        user = User(id=target.user_id, first_name=target.first_name or "",
                    last_name=target.last_name, username=target.username)
        ctx.session.add(user)
    async with global_mute_service.user_lock(target.user_id):
        await global_mute_service.activate(
            ctx.session, user_id=target.user_id, muted_by=ctx.user_id, reason=reason)
        # Commit before starting the independent rollout session so its per-chat
        # audit list cannot race this update transaction.
        await ctx.session.commit()
    await ctx.reply(
        f"🔇 کاربر <code>{target.user_id}</code> به‌صورت سراسری ساکت شد.\n"
        f"📌 {html.escape(reason)}\n"
        "↻ در سوپرگروه‌های شناخته‌شده‌ای اعمال می‌شود که ربات مدیرِ دارای حق محدودسازی باشد و مالک بات در آن‌ها حضور داشته باشد. سکوت در خود کانال امکان‌پذیر نیست.")
    asyncio.create_task(_global_mute_rollout(ctx.bot, target.user_id, ctx.chat_id))


@command("رفع سکوت سراسری", role="founder", category="owner",
         description="برداشتن سکوت سراسری", allow_in_private=True)
async def cmd_global_unmute(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_mute"):
        await ctx.reply("⛔️ این فرمان فقط برای مالک ربات یا دارندهٔ اختیار «سکوت سراسری» در همین چت مجاز است.")
        return
    target = await ctx.target()
    user_id = target.user_id if target and not target.error else None
    if not user_id and ctx.arg_text:
        from ..core.normalization import normalize_digits

        digits = normalize_digits(ctx.arg_text.strip(), to="ascii")
        if digits.isdigit() and 5 <= len(digits) <= 15:
            user_id = int(digits)
    if not user_id:
        await ctx.reply((target.error if target and target.error else None)
                        or "❌ کاربر هدف مشخص نشد.")
        return
    async with global_mute_service.user_lock(user_id):
        snapshot = await global_mute_service.deactivate(ctx.session, user_id=user_id)
        if snapshot is not None:
            await ctx.session.commit()
    if snapshot is None:
        await ctx.reply("ℹ️ این کاربر در سکوت سراسری نبود.")
        return
    await ctx.reply(f"🔊 سکوت سراسری کاربر <code>{user_id}</code> لغو شد؛ "
                    "پاک‌سازی محدودیت‌های ثبت‌شده در سوپرگروه‌ها در حال انجام است.")
    asyncio.create_task(_global_mute_rollout(
        ctx.bot, user_id, ctx.chat_id, snapshot=snapshot))


@command("لیست سکوت سراسری", role="founder", category="owner",
         description="نمایش کاربران دارای سکوت سراسری", allow_in_private=True)
async def cmd_global_mute_list(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "global_mute"):
        await ctx.reply("⛔️ این فهرست فقط برای مالک ربات یا دارندهٔ اختیار «سکوت سراسری» در همین چت است.")
        return
    result = await ctx.session.execute(
        select(GlobalMute).where(GlobalMute.active.is_(True))
        .order_by(GlobalMute.created_at.desc()).limit(50))
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("✅ هیچ سکوت سراسری فعالی وجود ندارد.")
        return
    lines = ["🔇 <b>سکوت‌های سراسری</b>", ""]
    for row in rows:
        lines.append(f"• <code>{row.user_id}</code> — 📌 {html.escape(row.reason or '—')}\n"
                     f"   🕒 {persian_datetime(row.created_at)}")
    await ctx.reply("\n".join(lines))


@command("دسترسی روت", role="founder", category="owner",
         description="اعطای دسترسی روت محدود به یک گروه/کانال",
         usage="دسترسی روت @username یا شناسه", allow_in_private=True)
async def cmd_root_grant(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    if ctx.message.chat.type == "private":
        if not is_owner:
            await ctx.reply("⛔️ واگذاری دسترسی از خصوصی فقط برای مالک ربات است.")
            return
        if ctx.message.reply_to_message and ctx.message.reply_to_message.from_user:
            target_ref = str(ctx.message.reply_to_message.from_user.id)
        elif len(ctx.args) == 1:
            target_ref = ctx.args[0]
        else:
            await ctx.reply("⚠️ شناسهٔ عددی یا نام‌کاربری هدف را بدهید.\n"
                            "نمونه: <code>دسترسی روت @username</code>")
            return
        chats = await owner_access.known_active_chats(ctx.session)
        if not chats:
            await ctx.reply("ℹ️ چت فعال و شناخته‌شده‌ای برای انتخاب ثبت نشده است.")
            return
        try:
            draft = await owner_access.create_root_draft(
                ctx.session, owner_id=ctx.user_id, target_ref=target_ref)
        except owner_access.RootAccessError as exc:
            await ctx.reply(str(exc))
            return
        keyboard = owner_access.root_picker_keyboard(draft, chats, 0)
        await ctx.reply(owner_access.root_picker_text(draft, chats, 0), reply_markup=keyboard)
        return

    if ctx.message.chat.type not in {"group", "supergroup"}:
        await ctx.reply("⚠️ برای انتخاب کانال، این دستور را در خصوصی ربات اجرا کنید.")
        return
    if not await _may_use_owner_permission(ctx, "root_manage"):
        await ctx.reply("⛔️ برای واگذاری اختیار روت در این گروه، مالک ربات یا دارندهٔ دسترسی «مدیریت روت» باشید.")
        return
    target = await ctx.target()
    if target.error or not target.user_id:
        await ctx.reply(target.error or "❌ کاربر هدف مشخص نشد.")
        return
    if target.user_id in {ctx.bot.id, ctx.user_id, settings.owner_id}:
        await ctx.reply("⛔️ ربات، خودتان یا مالک بات را نمی‌توان هدف واگذاری قرار داد.")
        return
    try:
        user_id, name, username = await owner_access.resolve_admin_target(
            ctx.bot, ctx.session, ctx.chat_id, str(target.user_id))
    except owner_access.RootAccessError as exc:
        await ctx.reply(str(exc))
        return
    try:
        draft = await owner_access.create_root_draft(
            ctx.session, owner_id=ctx.user_id, target_ref=str(user_id))
    except owner_access.RootAccessError as exc:
        await ctx.reply(str(exc))
        return
    db_user = await ctx.session.get(User, user_id)
    if db_user is not None:
        db_user.first_name = name[:128]
        db_user.username = username or target.username
    allowed = (list(owner_access.ROOT_PERMISSION_KEYS) if is_owner else
               await owner_access.permissions_for(
                   ctx.session, chat_id=ctx.chat_id, user_id=ctx.user_id))
    existing = await owner_access.permissions_for(
        ctx.session, chat_id=ctx.chat_id, user_id=user_id)
    selected = [permission for permission in existing if permission in allowed]
    chat = await ctx.session.get(Chat, ctx.chat_id)
    await ctx.reply(
        owner_access.root_permission_text(
            target_name=name, user_id=user_id, chat=chat,
            selected_permissions=selected, allowed_permissions=allowed),
        reply_markup=owner_access.root_permission_keyboard(
            draft, chat_id=ctx.chat_id, user_id=user_id,
            selected_permissions=selected, allowed_permissions=allowed))


@command("لغو دسترسی روت", role="founder", category="owner",
         description="لغو دسترسی روت محدود به چت",
         usage="لغو دسترسی روت @username یا شناسه", allow_in_private=True)
async def cmd_root_revoke(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    if ctx.message.chat.type == "private" and not is_owner:
        await ctx.reply("⛔️ لغو سراسری واگذاری‌ها از خصوصی فقط برای مالک ربات است.")
        return
    if ctx.message.chat.type not in {"private", "group", "supergroup"}:
        await ctx.reply("⚠️ این فرمان در خصوصی یا گروه قابل اجراست.")
        return
    if ctx.message.chat.type in {"group", "supergroup"} and not await _may_use_owner_permission(
            ctx, "root_manage"):
        await ctx.reply("⛔️ برای لغو دسترسی روت در این گروه، مالک ربات یا دارندهٔ اختیار «مدیریت روت» باشید.")
        return
    from ..core.normalization import normalize_digits
    from sqlalchemy import func

    if ctx.message.chat.type == "private":
        if ctx.message.reply_to_message and ctx.message.reply_to_message.from_user:
            user_id = int(ctx.message.reply_to_message.from_user.id)
        elif len(ctx.args) == 1:
            ref = ctx.args[0]
            normalized = normalize_digits(ref, to="ascii")
            if normalized.isdigit():
                user_id = int(normalized)
            else:
                username = ref.lstrip("@").lower()
                user = (await ctx.session.execute(
                    select(User).where(func.lower(User.username) == username).limit(1)
                )).scalars().first()
                if user is None:
                    await ctx.reply("❌ نام‌کاربری در کاربران شناخته‌شده نیست؛ "
                                    "از «فهرست دسترسی‌های روت» استفاده کنید.")
                    return
                user_id = int(user.id)
        else:
            await ctx.reply("⚠️ شناسه یا نام‌کاربری را بدهید؛ این فرمان در خصوصی همهٔ دسترسی‌های آن کاربر را لغو می‌کند.")
            return
        count = await owner_access.revoke_root_everywhere(ctx.session, user_id=user_id)
        await ctx.reply(f"🗑 {to_persian_digits(str(count))} دسترسی روت برای "
                        f"<code>{user_id}</code> لغو شد.")
        return

    target = await ctx.target()
    if target.error or not target.user_id:
        await ctx.reply(target.error or "❌ کاربر هدف مشخص نشد.")
        return
    removed = await owner_access.revoke_root(
        ctx.session, chat_id=ctx.chat_id, user_id=target.user_id)
    await ctx.reply("🗑 دسترسی روت این کاربر در همین چت لغو شد."
                    if removed else "ℹ️ این کاربر در همین چت دسترسی روت نداشت.")


@command("فهرست دسترسی‌های روت", "دسترسی‌های روت", role="founder",
         category="owner", description="نمایش دسترسی‌های روت",
         allow_in_private=True)
async def cmd_root_list(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    chat_scoped = ctx.message.chat.type in {"group", "supergroup"}
    if ctx.message.chat.type == "private" and not is_owner:
        await ctx.reply("⛔️ فهرست دسترسی‌های روت از خصوصی فقط برای مالک ربات است.")
        return
    if ctx.message.chat.type not in {"private", "group", "supergroup"}:
        await ctx.reply("⚠️ این فهرست در خصوصی یا گروه قابل نمایش است.")
        return
    if chat_scoped and not await _may_use_owner_permission(ctx, "root_manage"):
        await ctx.reply("⛔️ برای دیدن دسترسی‌های روت این گروه، مالک ربات یا دارندهٔ اختیار «مدیریت روت» باشید.")
        return
    if not chat_scoped and not is_owner:
        await ctx.reply("⛔️ این فهرست فقط برای مالک ربات است.")
        return
    from ..keyboards.factory import cb, danger, markup

    grants = await owner_access.list_root_grants(
        ctx.session, chat_id=ctx.chat_id if chat_scoped else None)
    if not grants:
        await ctx.reply("✅ در این محدوده دسترسی روتی ثبت نشده است." if chat_scoped
                        else "✅ هیچ دسترسی روتی ثبت نشده است.")
        return
    lines = ["🔐 <b>دسترسی‌های روت</b>", ""]
    buttons = []
    for grant, chat, user in grants[:50]:
        member_name = (f"{user.first_name or ''} {user.last_name or ''}".strip()
                       or (f"@{user.username}" if user.username else str(user.id)))
        chat_name = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
        labels = owner_access.selected_permissions_text(grant.permissions)
        lines.append(f"• {html.escape(member_name)} — {html.escape(chat_name)}\n"
                     f"   🔑 {html.escape(labels)}")
        buttons.append([danger(
            f"🗑 {member_name[:18]} · {chat_name[:18]}",
            cb("rt", "revoke", chat.id, grant.user_id))])
    if len(grants) > 50:
        lines.append(f"\n… {to_persian_digits(str(len(grants) - 50))} مورد دیگر در فهرست هست.")
    await ctx.reply("\n".join(lines), reply_markup=markup(buttons))


@command("ارسال همگانی", "همگانی", role="founder", category="owner",
         description="ارسال انتخابی به گروه‌ها و کانال‌های فعال",
         usage="ارسال همگانی «متن» یا پاسخ به پیام", allow_in_private=True)
async def cmd_broadcast(ctx: CommandContext) -> None:
    from ..services import broadcast as broadcast_service, owner_access, owner_broadcast
    from ..keyboards.factory import success, danger, markup

    user_id = ctx.user_id
    is_owner = owner_broadcast.is_bot_owner(user_id)
    delegated = not is_owner
    message_chat_type = ctx.message.chat.type
    chats = await owner_broadcast.effective_chats(
        ctx.session, user_id, message_chat_id=ctx.chat_id,
        message_chat_type=message_chat_type)
    if message_chat_type in {"group", "supergroup"}:
        if not chats:
            await ctx.reply("⛔️ در این چت دسترسی ارسال ندارید. در گروه، عملیات فقط همان‌جا انجام می‌شود؛ "
                            "در خصوصی، مقصدهای مجاز را انتخاب کنید.")
            return
        if delegated and not await owner_access.is_current_admin_or_creator(
                ctx.bot, ctx.chat_id, user_id):
            await ctx.reply("⛔️ دسترسی روت در این چت نیازمند مدیر/مالک بودن فعلی شماست.")
            return
    elif message_chat_type != "private":
        await ctx.reply("🔒 برای این چت، فرمان را در گروه یا خصوصی ربات اجرا کنید.")
        return
    elif delegated and not chats:
        await ctx.reply("⛔️ دسترسی ارسال همگانی ندارید؛ در خصوصی فقط چت‌های واگذارشده و فعال نمایش داده می‌شوند.")
        return

    source = ctx.message.reply_to_message
    text = broadcast_service.extract_broadcast_text(ctx.raw_text, ctx.command)
    if source is None and not text:
        state = await owner_broadcast.get_bot_settings(ctx.session)
        destination_hint = ("در گروه، ارسال فقط به همان گروه محدود است.\n"
                            if message_chat_type in {"group", "supergroup"}
                            else "در خصوصی، مقصدها را از چت‌های فعالِ شناخته‌شده انتخاب می‌کنید.\n")
        message_text = (
            "⚠️ روی پیام موردنظر ریپلای کنید یا متن را داخل گیومه بنویسید.\n"
            "نمونه: <code>ارسال همگانی &quot;سلام به همه&quot;</code>\n"
            f"{destination_hint}\n"
            f"وضعیت قابلیت: {'🟢 روشن' if state.broadcast_enabled else '🔴 خاموش'}")
        keyboard = (owner_broadcast.switch_keyboard(
            state.broadcast_enabled, int(state.broadcast_report_delete_after or 0))
                    if is_owner else None)
        await ctx.reply(message_text, reply_markup=keyboard)
        return

    source_ids: list[int] | None = None
    source_chat_id: int | None = None
    if source is not None:
        source_chat_id = int(source.chat.id)
        source_ids = [int(source.message_id)]
        if source.media_group_id:
            cached_ids = broadcast_service.media_group_cache.get_message_ids(
                source.chat.id, str(source.media_group_id))
            if cached_ids:
                source_ids = [int(item) for item in cached_ids]

    local_chat = message_chat_type in {"group", "supergroup"}
    draft = await owner_broadcast.create_draft(
        ctx.session, owner_id=user_id, source_chat_id=source_chat_id,
        source_message_ids=source_ids, text_body=text or None,
        selected_chat_ids=[int(chat.id) for chat in chats] if local_chat else [])
    state = await owner_broadcast.get_bot_settings(ctx.session)
    chats = await owner_broadcast.effective_chats(
        ctx.session, user_id, message_chat_id=ctx.chat_id,
        message_chat_type=message_chat_type)
    await ctx.reply(
        owner_broadcast._selection_text(draft, chats, bool(state.broadcast_enabled),
                                        delegated=delegated),
        reply_markup=owner_broadcast.selection_keyboard(
            draft, chats, bool(state.broadcast_enabled), delegated=delegated,
            report_delete_after=int(state.broadcast_report_delete_after or 0)))


@command("آمار ربات", role="founder", category="owner", description="آمار کلی ربات",
         allow_in_private=True)
async def cmd_bot_stats(ctx: CommandContext) -> None:
    if not await _may_use_owner_permission(ctx, "bot_stats"):
        await ctx.reply("⛔️ آمار کلی فقط برای مالک ربات یا دارندهٔ اختیار «آمار ربات» در همین چت است.")
        return
    chats = await ctx.session.scalar(select(func.count(Chat.id))) or 0
    active_chats = await ctx.session.scalar(
        select(func.count(Chat.id)).where(Chat.is_active.is_(True))) or 0
    users = await ctx.session.scalar(select(func.count(User.id))) or 0
    actions = await ctx.session.scalar(select(func.count(ModerationAction.id))) or 0
    members = await ctx.session.scalar(
        select(func.coalesce(func.sum(Chat.member_count), 0))) or 0
    await ctx.reply("\n".join([
        "📊 <b>آمار ربات</b>",
        "",
        f"💬 چت‌های ثبت‌شده: {to_persian_digits(str(chats))}",
        f"🟢 چت‌های فعالِ ثبت‌شده: {to_persian_digits(str(active_chats))}",
        "ℹ️ رابط برنامه‌نویسی ربات تلگرام (Bot API) فهرست کامل چت‌ها را نمی‌دهد؛ آمار بر پایهٔ چت‌های شناخته‌شده است.",
        f"👥 مجموع اعضا: {to_persian_digits(str(members))}",
        f"🙋 کاربران شناخته‌شده: {to_persian_digits(str(users))}",
        f"🛡 تعداد عملیات مدیریتی: {to_persian_digits(str(actions))}",
    ]))


@command("ترک گروه", role="founder", category="owner", description="خروج ربات از یک گروه",
         allow_in_private=True)
async def cmd_leave_chat(ctx: CommandContext) -> None:
    is_owner = ctx.actor.is_bot_owner
    if not is_owner:
        if not ctx.is_group or not await _may_use_owner_permission(ctx, "leave_chat"):
            await ctx.reply("⛔️ خروج ربات فقط برای مالک ربات یا دارندهٔ اختیار «خروج ربات» در همین گروه مجاز است.")
            return
    import re
    from ..core.normalization import normalize_digits

    raw_id = normalize_digits(ctx.arg_text, to="ascii").strip()
    if not re.fullmatch(r"-?\d{5,15}", raw_id):
        await ctx.reply("⚠️ شناسهٔ عددی گروه را دقیق وارد کنید: "
                        "<code>ترک گروه -100123456789</code>")
        return
    chat_id = int(raw_id)
    if not is_owner and chat_id != int(ctx.chat_id):
        await ctx.reply("⛔️ این اختیار فقط اجازهٔ خروج ربات از همین گروه واگذارشده را می‌دهد.")
        return
    from ..core.errors import safe_call

    ok = await safe_call(lambda: ctx.bot.leave_chat(chat_id=chat_id), default=None,
                         context="leave_chat")
    if ok is None:
        await ctx.reply("⚠️ خروج انجام نشد.")
        return
    chat = await ctx.session.get(Chat, chat_id)
    if chat is not None:
        chat.is_active = False
    await ctx.session.flush()
    await ctx.reply(f"👋 ربات از گروه <code>{chat_id}</code> خارج شد.")
