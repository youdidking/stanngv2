"""Utility commands: pins, purge, tags, AFK, reports, staff, trust, logs,
scheduling, backup, connection and help."""

from __future__ import annotations

import html
import io
import json
import logging
from datetime import datetime

from sqlalchemy import select

from ..core.duration import parse_duration
from ..core.errors import safe_call
from ..core.normalization import normalize_digits, normalize_text, to_persian_digits
from ..core.timeutils import persian_datetime
from ..keyboards.menus import BOT_NAME, help_text
from ..db.models import (
    AuditLog,
    BotRole,
    Report,
    ScheduledMessage,
    TrustedUser,
    User,
)
from ..services import (
    afk as afk_service,
    backup as backup_service,
    connection as connection_service,
    disabled as disabled_service,
    entertainment,
    personal,
    pins as pin_service,
    purge as purge_service,
    tags as tag_service,
)
from ..services.chat_state import (get_member_state, get_settings, invalidate_member,
                                   invalidate_settings, set_bot_role, set_trust)
from ..services.permissions import is_anonymous_sender
from ..services.roles import can_manage_role
from .common import (CommandContext, display_name, guard_target, require, undo_keyboard,
                     user_html)
from .registry import command, registry

logger = logging.getLogger("armando.handlers.tools")


def _int_of(text: str, default: int | None = None) -> int | None:
    raw = normalize_digits(text or "", to="ascii")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return default
    try:
        return int(digits)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Pins
# --------------------------------------------------------------------------- #
@command("پین", "سنجاق", role="helper", permission="pin", category="messages",
         description="پین کردن پیام", usage="پین (با ریپلای) یا: پین متن دلخواه")
async def cmd_pin(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="pin"):
        return
    reply = ctx.message.reply_to_message
    if reply is not None:
        ok = await pin_service.pin_message(ctx.bot, ctx.chat_id, reply.message_id, silent=True)
        await ctx.reply("📌 پیام پین شد." if ok else "⚠️ پین انجام نشد (دسترسی ربات را بررسی کنید).",
                        reply_to=False)
        return
    text = ctx.arg_text.strip()
    if not text:
        await ctx.reply("📌 روی یک پیام ریپلای کنید یا متن بنویسید: <code>پین متن مورد نظر</code>")
        return
    message = await pin_service.send_and_pin(ctx.bot, ctx.chat_id, f"📌 {text}")
    await ctx.reply("📌 پیام ارسال و پین شد." if message else "⚠️ پین انجام نشد.", reply_to=False)


@command("حذف پین", "برداشتن پین", role="helper", permission="pin",
         category="messages", description="برداشتن پین")
async def cmd_unpin(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="pin"):
        return
    reply = ctx.message.reply_to_message
    ok = await pin_service.unpin_message(ctx.bot, ctx.chat_id,
                                         reply.message_id if reply else None)
    await ctx.reply("📍 پین برداشته شد." if ok else "⚠️ عملیات انجام نشد.", reply_to=False)


@command("پین فعلی", "نمایش پین", role="member", category="messages",
         description="نمایش پیام پین‌شده")
async def cmd_get_pin(ctx: CommandContext) -> None:
    pinned = await pin_service.get_pinned(ctx.bot, ctx.chat_id)
    if pinned is None:
        await ctx.reply("ℹ️ پیام پین‌شده‌ای وجود ندارد.")
        return
    await ctx.reply(f"📌 <b>پیام پین‌شده</b>\n\n{pinned.text or pinned.caption or '—'}")


@command("حذف همه پین‌ها", role="admin", permission="pin", category="messages",
         description="حذف تمام پیام‌های پین‌شده")
async def cmd_unpin_all(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="pin"):
        return
    ok = await pin_service.unpin_all(ctx.bot, ctx.chat_id)
    await ctx.reply("🧹 همه پیام‌های پین‌شده حذف شدند." if ok else "⚠️ عملیات انجام نشد.")


# --------------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------------- #
@command("پاکسازی", "پاک کردن", "حذف پیام‌ها", role="cleaner", permission="purge",
         category="messages", description="پاکسازی پیام‌ها", usage="پاکسازی ۱۰۰")
async def cmd_purge(ctx: CommandContext) -> None:
    if not await require(ctx, role="cleaner", permission="purge"):
        return
    reply = ctx.message.reply_to_message
    count = _int_of(ctx.arg_text, 0) or 0
    if reply is not None:
        result = await purge_service.purge_range(ctx.bot, ctx.chat_id, reply.message_id,
                                                 ctx.message.message_id)
        await ctx.delete_invocation()
        await ctx.reply(purge_service.purge_summary(result), reply_to=False)
        from ..services.moderation import record_action

        await record_action(ctx.session, chat_id=ctx.chat_id, action="purge",
                            target_id=ctx.user_id, actor_id=ctx.user_id,
                            reason=f"{result.deleted} پیام", source="command")
        return
    if count <= 0:
        await ctx.reply("🧹 روی پیام مورد نظر ریپلای کنید یا تعداد بنویسید: <code>پاکسازی ۱۰۰</code>")
        return
    result = await purge_service.purge_count(ctx.bot, ctx.chat_id, count, ctx.message.message_id)
    await ctx.delete_invocation()
    await ctx.reply(purge_service.purge_summary(result), reply_to=False)



# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
@command("تگ", "تنظیم تگ", "تغییر تگ", role="admin", permission="manage",
         category="tags", description="تنظیم تگ کاربر (با ریپلای)", usage="تگ مدیر فروش")
async def cmd_set_tag(ctx: CommandContext) -> None:
    target = await ctx.target()
    is_self_request = normalize_text(ctx.command, mode="command").startswith("تگ من")
    if is_self_request and bool(ctx.settings.get("tag_allow_user_edit")):
        if not await require(ctx, role="member"):
            return
        try:
            tag = await tag_service.set_tag(ctx.session, ctx.chat_id, ctx.user_id, ctx.arg_text)
        except ValueError as exc:
            await ctx.reply(str(exc))
            return
        mirrored, reason = await tag_service.apply_member_tag(ctx.bot, ctx.chat_id,
                                                              ctx.user_id, tag)
        suffix = "\n✅ در لیست اعضای تلگرام هم نمایش داده می‌شود." if mirrored else f"\n{reason}"
        await ctx.reply(f"🏷 تگ شما تنظیم شد: <code>{tag}</code>{suffix}")
        return
    if not await require(ctx, role="admin", permission="manage"):
        return
    if target is None or not await guard_target(ctx, target):
        return
    try:
        tag = await tag_service.set_tag(ctx.session, ctx.chat_id, target.user_id, ctx.arg_text)
    except ValueError as exc:
        await ctx.reply(str(exc))
        return
    if tag is None:
        await tag_service.apply_member_tag(ctx.bot, ctx.chat_id, target.user_id, None)
        await ctx.reply("🗑 تگ کاربر حذف شد.")
    else:
        mirrored, reason = await tag_service.apply_member_tag(ctx.bot, ctx.chat_id,
                                                             target.user_id, tag)
        suffix = "\n✅ در لیست اعضای تلگرام هم نمایش داده می‌شود." if mirrored else f"\n{reason}"
        await ctx.reply(f"🏷 تگ {user_html(target.user_id, target.full_name)} تنظیم شد: "
                        f"<code>{tag}</code>{suffix}")


@command("تگ من", role="member", category="tags", description="تنظیم تگ شخصی (در صورت اجازه مدیر)")
async def cmd_my_tag(ctx: CommandContext) -> None:
    await cmd_set_tag(ctx)


@command("حذف تگ", role="admin", permission="manage", category="tags",
         description="حذف تگ کاربر")
async def cmd_del_tag(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    target = await ctx.target()
    if target is None or not await guard_target(ctx, target):
        return
    await tag_service.set_tag(ctx.session, ctx.chat_id, target.user_id, None)
    await tag_service.apply_member_tag(ctx.bot, ctx.chat_id, target.user_id, None)
    await ctx.reply(f"🗑 تگ {user_html(target.user_id, target.full_name)} حذف شد.")


@command("حذف از تگ", "حذف از تگ خود", role="admin", permission="manage",
         category="tags", description="استثناکردن خود یا عضو از تگ همه", group_only=True)
async def cmd_exclude_from_mention_all(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    ids: list[int] = []
    caller = ctx.message.from_user
    if caller is not None and not is_anonymous_sender(caller):
        ids.append(ctx.user_id)

    self_only = normalize_text(ctx.command, mode="command") == normalize_text(
        "حذف از تگ خود", mode="command")
    if ctx.message.reply_to_message is not None and not self_only:
        target = await ctx.target()
        if target.error or not target.user_id:
            await ctx.reply(target.error or "❌ کاربر هدف مشخص نشد.")
            return
        if target.user_id not in ids:
            ids.append(target.user_id)

    if not ids:
        await ctx.reply("⚠️ هویت ادمینِ ارسال‌کننده مشخص نیست؛ روی پیام یک عضو ریپلای کنید.")
        return

    for user_id in ids:
        state = await get_member_state(ctx.session, ctx.chat_id, user_id, create=True)
        state.mention_opt_out = True
        state.updated_at = datetime.utcnow()
    await ctx.session.flush()

    lines = ["🏷 از این پس اعضای زیر در «تگ همه» منشن نمی‌شوند:"]
    for user_id in ids:
        lines.append(f"• {user_html(user_id, await display_name(ctx.session, user_id))}")
    lines.append("\nبرای بازگردانی، روی پیام عضو ریپلای کنید و بنویسید "
                 "<code>افزودن به تگ</code>؛ برای خودتان: <code>افزودن به تگ خود</code>.")
    await ctx.reply("\n".join(lines))


@command("افزودن به تگ", "افزودن به تگ خود", role="admin", permission="manage",
         category="tags", description="بازگرداندن خود یا عضو به تگ همه", group_only=True)
async def cmd_restore_mention_all(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    self_only = normalize_text(ctx.command, mode="command") == normalize_text(
        "افزودن به تگ خود", mode="command")
    if ctx.message.reply_to_message is not None and not self_only:
        target = await ctx.target()
        if target.error or not target.user_id:
            await ctx.reply(target.error or "❌ کاربر هدف مشخص نشد.")
            return
        user_ids = [target.user_id]
    else:
        if is_anonymous_sender(ctx.message.from_user):
            await ctx.reply("⚠️ برای بازگردانی، روی پیام عضو ریپلای کنید.")
            return
        user_ids = [ctx.user_id]

    for user_id in user_ids:
        state = await get_member_state(ctx.session, ctx.chat_id, user_id, create=True)
        state.mention_opt_out = False
        state.updated_at = datetime.utcnow()
    await ctx.session.flush()
    user_id = user_ids[0]
    await ctx.reply(f"✅ {user_html(user_id, await display_name(ctx.session, user_id))} "
                    "دوباره در «تگ همه» منشن می‌شود.")


@command("لیست تگ‌ها", "تگ‌ها", role="member", category="tags", description="نمایش تگ اعضا")
async def cmd_tag_list(ctx: CommandContext) -> None:
    rows = await tag_service.tags_of_chat(ctx.session, ctx.chat_id)
    if not rows:
        await ctx.reply("🏷 هیچ تگی ثبت نشده است.")
        return
    enriched = []
    for user_id, tag in rows:
        name = await display_name(ctx.session, user_id)
        enriched.append((user_id, name, tag))
    await ctx.reply(tag_service.tags_text(enriched))


@command("اجازه تگ", role="admin", permission="manage", category="tags",
         description="اجازه دادن به اعضا برای تغییر تگ خودشان")
async def cmd_allow_self_tag(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.tag_allow_user_edit = not bool(settings_obj.tag_allow_user_edit)
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🟢 اعضا می‌توانند تگ خود را تغییر دهند."
                    if settings_obj.tag_allow_user_edit else
                    "🔴 تغییر تگ فقط توسط مدیران انجام می‌شود.")


# --------------------------------------------------------------------------- #
# AFK
# --------------------------------------------------------------------------- #
@command("برم افک", "برم ای اف کی", "افک", "ای اف کی", "من رفتم", role="member",
         category="afk", description="تنظیم وضعیت AFK", usage="برم افک مشغول کارم")
async def cmd_afk(ctx: CommandContext) -> None:
    if not bool(ctx.settings.get("afk_enabled", True)):
        return
    await afk_service.set_afk(ctx.session, ctx.user_id, ctx.arg_text)
    await ctx.reply("💤 شما در وضعیت AFK قرار گرفتید.\n"
                    "با ارسال اولین پیام عادی، به‌طور خودکار باز می‌گردید.")


@command("برگشتم", "حذف افک", "لغو افک", role="member", category="afk",
         description="خروج از وضعیت AFK")
async def cmd_unafk(ctx: CommandContext) -> None:
    removed = await afk_service.clear_afk(ctx.session, ctx.user_id)
    await ctx.reply("✅ به جمع برگشتید." if removed else "ℹ️ شما در وضعیت AFK نبودید.")


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@command("گزارش", "ریپورت", "شکایت", "گزارش این پیام", role="member",
         category="report", description="گزارش یک پیام به مدیران")
async def cmd_report(ctx: CommandContext) -> None:
    if not bool(ctx.settings.get("report_enabled", True)):
        return
    from ..core.ratelimit import report_limiter

    allowed, retry = report_limiter.check((ctx.chat_id, ctx.user_id))
    if not allowed:
        await ctx.reply(f"⏳ گزارش‌های زیادی فرستادید. {int(retry)} ثانیه دیگر تلاش کنید.")
        return
    reply = ctx.message.reply_to_message
    if reply is None:
        await ctx.reply("📢 روی پیام مورد نظر ریپلای کنید و بنویسید: <code>گزارش</code>")
        return
    target_user = reply.from_user
    if target_user is None:
        await ctx.reply("📢 این پیام فرستنده مشخصی ندارد.")
        return
    reason = ctx.arg_text.strip() or "بدون توضیح"
    from ..services import reports as report_service

    report = await report_service.create_report(
        ctx.session, chat_id=ctx.chat_id, reporter_id=ctx.user_id,
        target_id=target_user.id, message_id=reply.message_id, reason=reason)
    await report_service.notify_admins(
        ctx.bot, ctx.session, chat_id=ctx.chat_id, report=report,
        chat_title=ctx.chat_title, reporter_name=ctx.message.from_user.full_name,
        target_name=target_user.full_name,
        message_preview=(reply.text or reply.caption or ""))
    await ctx.reply("📢 گزارش شما برای مدیران ارسال شد. ممنون از همکاری.")
    await ctx.delete_invocation()


@command("گزارش‌ها", "لیست گزارش‌ها", role="moderator", category="report",
         description="نمایش گزارش‌های باز")
async def cmd_open_reports(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator"):
        return
    from ..services import reports as report_service

    reports = await report_service.open_reports(ctx.session, ctx.chat_id)
    if not reports:
        await ctx.reply("✅ هیچ گزارش بازی وجود ندارد.")
        return
    lines = [f"📢 <b>گزارش‌های باز</b> ({to_persian_digits(str(len(reports)))} مورد)", ""]
    for report_item in reports:
        reporter = await display_name(ctx.session, report_item.reporter_id)
        target = await display_name(ctx.session, report_item.target_id) if report_item.target_id else "—"
        lines.append(f"• #{report_item.id} — از {reporter} درباره {target}\n"
                     f"   📌 {report_item.reason}\n   🕒 {persian_datetime(report_item.created_at)}")
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------- #
# Staff & trust
# --------------------------------------------------------------------------- #
@command("لیست مدیران", "مدیران", "ادمین‌ها", role="member", category="staff",
         description="نمایش مدیران گروه")
async def cmd_admins(ctx: CommandContext) -> None:
    from ..services.roles import ROLE_NAMES_FA

    try:
        admins = await ctx.bot.get_chat_administrators(chat_id=ctx.chat_id)
    except Exception:  # noqa: BLE001
        admins = []
    lines = ["👮 <b>مدیران تلگرام</b>", ""]
    for admin in admins:
        user = admin.user
        status = "مالک" if admin.status == "creator" else "مدیر"
        lines.append(f"• {user_html(user.id, user.full_name)} — {status}")
    staff_result = await ctx.session.execute(
        select(BotRole).where(BotRole.chat_id == ctx.chat_id)
    )
    staff = list(staff_result.scalars().all())
    if staff:
        lines.append("")
        lines.append("🛡 <b>کادر ربات</b>")
        for row in staff:
            name = await display_name(ctx.session, row.user_id)
            lines.append(f"• {user_html(row.user_id, name)} — {ROLE_NAMES_FA.get(row.role, row.role)}")
    await ctx.reply("\n".join(lines))


@command("لیست کادر", "کادر ربات", "نقش‌ها", role="moderator", category="staff",
         description="نمایش نقش‌های داخلی ربات")
async def cmd_staff_list(ctx: CommandContext) -> None:
    from ..services.roles import ROLE_NAMES_FA

    result = await ctx.session.execute(
        select(BotRole).where(BotRole.chat_id == ctx.chat_id).order_by(BotRole.role)
    )
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("👮 هیچ نقش داخلی ثبت نشده است.")
        return
    lines = ["🛡 <b>نقش‌های داخلی</b>", ""]
    for row in rows:
        name = await display_name(ctx.session, row.user_id)
        lines.append(f"• {user_html(row.user_id, name)} — {ROLE_NAMES_FA.get(row.role, row.role)}")
    await ctx.reply("\n".join(lines))


@command("لیست ویژه‌ها", "کاربران ویژه", "ویژه‌ها", role="member", category="staff",
         description="نمایش کاربران ویژه")
async def cmd_trusted_list(ctx: CommandContext) -> None:
    result = await ctx.session.execute(
        select(TrustedUser).where(TrustedUser.chat_id == ctx.chat_id)
    )
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("⭐️ هیچ کاربر ویژه‌ای ثبت نشده است.")
        return
    lines = ["⭐️ <b>کاربران ویژه</b>", ""]
    for row in rows:
        name = await display_name(ctx.session, row.user_id)
        bypass = ", ".join(row.bypass or []) or "بدون معافیت"
        lines.append(f"• {user_html(row.user_id, name)} — معافیت‌ها: {bypass}")
    await ctx.reply("\n".join(lines))


@command("معافیت‌ها", role="admin", permission="manage", category="staff",
         description="تنظیم معافیت‌های کاربران ویژه", usage="معافیت‌ها قفل‌ها فیلترها")
async def cmd_trust_bypass(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    options = {
        "قفلها": "locks", "قفل‌ها": "locks", "قفل": "locks",
        "فیلترها": "filters", "فیلتر": "filters",
        "ضدفلاود": "antiflood", "فلاود": "antiflood",
        "ضداسپم": "antispam", "اسپم": "antispam",
        "زبان": "lang", "متن": "lang",
        "کپچا": "captcha",
        "همه": "all", "همهچیز": "all",
    }
    chosen = []
    for token in ctx.args:
        key = options.get(normalize_text(token, mode="command"))
        if key and key not in chosen:
            chosen.append(key)
    if not chosen:
        await ctx.reply("⚠️ گزینه‌ها: قفل‌ها، فیلترها، ضدفلاود، ضداسپم، زبان، کپچا، همه")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    current = set(settings_obj.trust_bypass or [])
    for key in chosen:
        if key in current:
            current.discard(key)
        else:
            current.add(key)
    settings_obj.trust_bypass = sorted(current)
    invalidate_settings(ctx.chat_id)
    await ctx.reply("✅ معافیت‌های کاربران ویژه به‌روز شد: " + ", ".join(sorted(current)))


def _register_role_commands() -> None:
    """Register ``افزودن <نقش>`` / ``عزل <نقش>`` commands for every role."""
    role_persian = {
        "cofounder": ("هم‌بنیان‌گذار", "همبنیانگذار", "معاون"),
        "super_admin": ("مدیر ارشد", "مدیرارشد"),
        "moderator": ("ناظر",),
        "helper": ("کمک‌یار", "کمکیار", "کمک يار"),
        "muter": ("ساکت‌کننده", "ساکت کننده"),
        "cleaner": ("پاکساز",),
        "trusted": ("ویژه", "کاربر ویژه"),
        "admin": ("مدیر",),
    }
    minimum_role = {
        "cofounder": "founder",
        "super_admin": "cofounder",
        "admin": "cofounder",
        "moderator": "admin",
        "helper": "admin",
        "muter": "admin",
        "cleaner": "admin",
        "trusted": "admin",
    }

    for role_key, names in role_persian.items():
        for name in names[:1]:
            def make_add(key: str, label: str, min_role: str):
                async def handler(ctx: CommandContext) -> None:
                    if not await require(ctx, role=min_role, permission="manage"):
                        return
                    target = await ctx.target()
                    if target is None or not await guard_target(ctx, target):
                        return
                    from ..services.chat_state import get_member_state_snapshot

                    snapshot = await get_member_state_snapshot(ctx.session, ctx.chat_id,
                                                               target.user_id)
                    if not can_manage_role(ctx.actor.effective_role or "member",
                                           snapshot.get("bot_role")):
                        await ctx.reply("⛔️ نمی‌توانید نقش این کاربر را تغییر دهید.")
                        return
                    if key == "trusted":
                        await set_trust(ctx.session, ctx.chat_id, target.user_id, True,
                                        list(ctx.settings.get("trust_bypass") or []))
                    else:
                        await set_bot_role(ctx.session, ctx.chat_id, target.user_id, key,
                                           assigned_by=ctx.user_id)
                    from ..services.audit import audit_and_log
                    from ..services.roles import ROLE_NAMES_FA

                    await audit_and_log(
                        ctx.session, ctx.bot, chat_id=ctx.chat_id, action="staff_add",
                        actor_id=ctx.user_id, target_id=target.user_id,
                        actor_name=user_html(ctx.user_id, ctx.message.from_user.full_name),
                        target_name=user_html(target.user_id, target.full_name),
                        chat_title=ctx.chat_title, reason=ROLE_NAMES_FA.get(key, key))
                    await ctx.reply(f"✅ {user_html(target.user_id, target.full_name)} "
                                    f"با نقش «{ROLE_NAMES_FA.get(key, key)}» ثبت شد.")

                return handler

            def make_remove(key: str, label: str, min_role: str):
                async def handler(ctx: CommandContext) -> None:
                    if not await require(ctx, role=min_role, permission="manage"):
                        return
                    target = await ctx.target()
                    if target is None or not target.user_id:
                        await ctx.reply("❌ کاربر هدف مشخص نشد.")
                        return
                    if key == "trusted":
                        await set_trust(ctx.session, ctx.chat_id, target.user_id, False, [])
                    else:
                        from ..services.chat_state import get_member_state_snapshot

                        snapshot = await get_member_state_snapshot(ctx.session, ctx.chat_id,
                                                                   target.user_id)
                        if not can_manage_role(ctx.actor.effective_role or "member",
                                               snapshot.get("bot_role")):
                            await ctx.reply("⛔️ نمی‌توانید نقش این کاربر را تغییر دهید.")
                            return
                        await set_bot_role(ctx.session, ctx.chat_id, target.user_id, None,
                                           assigned_by=ctx.user_id)
                    await ctx.reply(f"🚫 نقش {user_html(target.user_id, target.full_name)} حذف شد.")

                return handler

            registry.register(type(registry.commands[0])(
                phrases=(f"افزودن {name}", f"اضافه کردن {name}"),
                handler=make_add(role_key, name, minimum_role[role_key]),
                role=minimum_role[role_key],
                permission="manage",
                category="staff",
                description=f"افزودن {name}",
                usage=f"افزودن {name} (با ریپلای)",
            ))
            registry.register(type(registry.commands[0])(
                phrases=(f"عزل {name}", f"حذف {name}", f"برکناری {name}"),
                handler=make_remove(role_key, name, minimum_role[role_key]),
                role=minimum_role[role_key],
                permission="manage",
                category="staff",
                description=f"عزل {name}",
                usage=f"عزل {name} (با ریپلای)",
            ))


_register_role_commands()


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
@command("تنظیم لاگ", "تنظیم کانال لاگ", role="admin", permission="manage",
         category="staff", description="تنظیم لاگ روی گروه/کانال فعلی")
async def cmd_set_log(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.log_chat_id = ctx.chat_id
    settings_obj.log_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply("📜 لاگ‌های مدیریتی این گروه در همین چت ثبت می‌شوند.")


@command("حذف لاگ", "قطع لاگ", role="admin", permission="manage", category="staff",
         description="حذف کانال لاگ")
async def cmd_clear_log(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.log_chat_id = None
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🗑 کانال لاگ حذف شد.")


@command("لاگ‌ها", "تاریخچه مدیریت", role="moderator", category="staff",
         description="نمایش آخرین رویدادهای مدیریتی")
async def cmd_logs(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission=None):
        return
    result = await ctx.session.execute(
        select(AuditLog).where(AuditLog.chat_id == ctx.chat_id)
        .order_by(AuditLog.created_at.desc()).limit(20)
    )
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("📜 رویدادی ثبت نشده است.")
        return
    from ..services.audit import action_label

    lines = ["📜 <b>آخرین رویدادها</b>", ""]
    for item in rows:
        target = await display_name(ctx.session, item.target_id) if item.target_id else "—"
        actor = await display_name(ctx.session, item.actor_id) if item.actor_id else "سیستم"
        lines.append(f"• {action_label(item.action)} — {actor} ➜ {target}\n"
                     f"   🕒 {persian_datetime(item.created_at)}"
                     + (f" | 📌 {item.reason}" if item.reason else ""))
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------- #
# Scheduled messages
# --------------------------------------------------------------------------- #
@command("زمان‌بندی", "تنظیم زمان‌بندی", role="admin", permission="manage",
         category="messages", description="زمان‌بندی پیام تکرارشونده",
         usage="زمان‌بندی ۲۴ساعت متن پیام | زمان‌بندی روزانه ۰۹:۰۰ متن")
async def cmd_schedule(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    if len(ctx.args) < 2:
        await ctx.reply("⏰ قالب‌ها:\n"
                        "• <code>زمان‌بندی ۲۴ساعت متن پیام</code>\n"
                        "• <code>زمان‌بندی روزانه ۰۹:۰۰ متن پیام</code>\n"
                        "• <code>زمان‌بندی هفتگی ۵ ۱۰:۰۰ متن پیام</code>")
        return
    from ..core.duration import parse_duration
    from ..core.timeutils import parse_hhmm

    spec = ctx.args[0]
    repeat = parse_duration(spec)
    cron = None
    if repeat is None:
        parsed_time = parse_hhmm(spec)
        if parsed_time is not None:
            cron = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"
        elif normalize_text(spec, mode="command").startswith("روزانه") and len(ctx.args) > 1:
            parsed_time = parse_hhmm(ctx.args[1])
            if parsed_time is None:
                await ctx.reply("❌ زمان نامعتبر است.")
                return
            cron = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"
            ctx.args = ctx.args[1:]
        elif normalize_text(spec, mode="command").startswith("هفتگی") and len(ctx.args) > 2:
            weekday = _int_of(ctx.args[1], 0)
            parsed_time = parse_hhmm(ctx.args[2])
            if parsed_time is None or weekday is None:
                await ctx.reply("❌ قالب هفتگی: <code>زمان‌بندی هفتگی ۵ ۱۰:۰۰ متن</code>")
                return
            cron = f"weekly:{weekday}:{parsed_time[0]:02d}:{parsed_time[1]:02d}"
            ctx.args = ctx.args[2:]
        else:
            await ctx.reply("❌ قالب زمانی نامعتبر است.")
            return
    text = " ".join(ctx.args[1:]).strip()
    if not text:
        await ctx.reply("❌ متن پیام را بنویسید.")
        return
    from ..services.scheduler import compute_next_run

    row = ScheduledMessage(chat_id=ctx.chat_id, name=text[:32], text=text,
                           repeat_seconds=repeat, cron=cron, enabled=True,
                           created_by=ctx.user_id, created_at=datetime.utcnow())
    row.next_run_at = compute_next_run(row)
    ctx.session.add(row)
    await ctx.session.flush()
    when = persian_datetime(row.next_run_at) if row.next_run_at else "—"
    await ctx.reply(f"⏰ پیام زمان‌بندی شد.\n🕒 اجرای بعدی: {when}\n"
                    f"🔢 شناسه: <code>{row.id}</code>")


@command("لیست زمان‌بندی", "زمان‌بندی‌ها", role="admin", permission="manage",
         category="messages", description="نمایش پیام‌های زمان‌بندی‌شده")
async def cmd_schedule_list(ctx: CommandContext) -> None:
    result = await ctx.session.execute(
        select(ScheduledMessage).where(ScheduledMessage.chat_id == ctx.chat_id)
        .order_by(ScheduledMessage.id)
    )
    rows = list(result.scalars().all())
    if not rows:
        await ctx.reply("⏰ هیچ پیام زمان‌بندی‌شده‌ای وجود ندارد.")
        return
    lines = ["⏰ <b>پیام‌های زمان‌بندی‌شده</b>", ""]
    for row in rows:
        state = "🟢" if row.enabled else "🔴"
        when = persian_datetime(row.next_run_at) if row.next_run_at else "—"
        lines.append(f"{state} #{row.id} — {row.text[:40]}\n   🕒 بعدی: {when}")
    lines.append("\nبرای حذف: <code>حذف زمان‌بندی ۱</code>")
    await ctx.reply("\n".join(lines))


@command("حذف زمان‌بندی", role="admin", permission="manage", category="messages",
         description="حذف پیام زمان‌بندی‌شده")
async def cmd_schedule_delete(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    row_id = _int_of(ctx.arg_text)
    if row_id is None:
        await ctx.reply("⚠️ شناسه را وارد کنید: <code>حذف زمان‌بندی ۱</code>")
        return
    row = await ctx.session.get(ScheduledMessage, row_id)
    if row is None or row.chat_id != ctx.chat_id:
        await ctx.reply("ℹ️ پیامی با این شناسه پیدا نشد.")
        return
    await ctx.session.delete(row)
    await ctx.session.flush()
    await ctx.reply("🗑 پیام زمان‌بندی‌شده حذف شد.")


# --------------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------------- #
@command("پشتیبان‌گیری", "بکاپ", "پشتیبان گیری", role="admin", permission="manage",
         category="tools", description="دریافت فایل پشتیبان تنظیمات گروه")
async def cmd_backup(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    payload = await backup_service.export_chat(ctx.session, ctx.chat_id)
    content = backup_service.dumps(payload).encode("utf-8")
    filename = f"armando-backup-{ctx.chat_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M')}.json"
    from aiogram.types import BufferedInputFile

    document = BufferedInputFile(content, filename=filename)
    await safe_call(lambda: ctx.bot.send_document(
        chat_id=ctx.chat_id, document=document,
        caption=backup_service.backup_summary(payload)), context="send_backup")
    await ctx.reply("💾 فایل پشتیبان ارسال شد.\n"
                    "برای بازیابی، همین فایل را ریپلای کنید و بنویسید: <code>بازیابی</code>")


@command("بازیابی", "بازگردانی", "ریستور", role="admin", permission="manage",
         category="tools", description="بازیابی تنظیمات از فایل پشتیبان")
async def cmd_restore(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    reply = ctx.message.reply_to_message
    if reply is None or not reply.document:
        await ctx.reply("♻️ روی فایل پشتیبان ریپلای کنید و بنویسید: <code>بازیابی</code>")
        return
    try:
        file = await ctx.bot.get_file(reply.document.file_id)
        buffer = io.BytesIO()
        await ctx.bot.download_file(file.file_path, destination=buffer)
        payload = json.loads(buffer.getvalue().decode("utf-8"))
    except Exception:  # noqa: BLE001
        await ctx.reply("❌ فایل نامعتبر است یا قابل خواندن نیست.")
        return
    try:
        counters = await backup_service.import_chat(ctx.session, ctx.chat_id, payload)
    except ValueError as exc:
        await ctx.reply(str(exc))
        return
    lines = ["♻️ <b>بازیابی انجام شد</b>", ""]
    labels = {"settings": "تنظیمات", "locks": "قفل‌ها", "filters": "فیلترها",
              "notes": "یادداشت‌ها", "commands": "دستورات", "warnings": "اخطارها",
              "trusted": "کاربران ویژه", "roles": "نقش‌ها", "scheduled": "زمان‌بندی‌ها"}
    for key, value in counters.items():
        if value:
            lines.append(f"• {labels.get(key, key)}: {to_persian_digits(str(value))}")
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------- #
# Connection mode
# --------------------------------------------------------------------------- #
@command("اتصال", "اتصال به گروه", "مدیریت خصوصی", role="admin", category="tools",
         description="اتصال چت خصوصی به یک گروه", private_only=True)
async def cmd_connect(ctx: CommandContext) -> None:
    from ..services import connection_service
    from .panel import send_panel

    chats = await connection_service.available_chats(ctx.bot, ctx.session, ctx.user_id)
    if not chats:
        await ctx.reply("ℹ️ شما در هیچ گروهی مدیر نیستید (یا ربات هنوز شما را نشناخته است).\n"
                        "ابتدا در گروه دستور <code>پنل</code> را اجرا کنید.")
        return
    if ctx.arg_text.strip():
        from ..core.normalization import normalize_text

        wanted = normalize_text(ctx.arg_text, mode="command")
        for chat_id, title in chats:
            if wanted and wanted in normalize_text(title, mode="command"):
                if not await connection_service.is_authorized(ctx.bot, ctx.session,
                                                              ctx.user_id, chat_id):
                    await ctx.reply("⛔️ شما در این گروه مدیر نیستید.")
                    return
                await connection_service.set_active(ctx.session, ctx.user_id, chat_id)
                await ctx.reply(f"🔗 اتصال به «{title}» برقرار شد.")
                await send_panel(ctx.bot, chat_id, ctx.user_id, ctx.session,
                                 source_chat_id=ctx.chat_id)
                return
        await ctx.reply("ℹ️ گروهی با این نام پیدا نشد.")
        return
    from ..keyboards.factory import cb, markup, primary, row

    buttons = [primary(title, cb("conn", chat_id)) for chat_id, title in chats]
    await ctx.reply("🔗 یکی از گروه‌ها را انتخاب کنید:", reply_markup=markup([*row(*buttons)]))


@command("قطع اتصال", role="admin", category="tools", description="قطع اتصال خصوصی")
async def cmd_disconnect(ctx: CommandContext) -> None:
    removed = await connection_service.disconnect(ctx.session, ctx.user_id)
    await ctx.reply("🔌 اتصال قطع شد." if removed else "ℹ️ اتصالی فعال نبود.")


@command("اتصال فعلی", "وضعیت اتصال", role="admin", category="tools",
         description="نمایش گروه متصل شده")
async def cmd_connection_status(ctx: CommandContext) -> None:
    from ..db.models import Chat

    chat_id = await connection_service.get_active(ctx.session, ctx.user_id)
    if not chat_id:
        await ctx.reply("ℹ️ اتصالی فعال نیست. در گروه دستور <code>پنل</code> را بزنید و سپس در "
                        "چت خصوصی <code>اتصال</code> را اجرا کنید.")
        return
    chat = await ctx.session.get(Chat, chat_id)
    await ctx.reply(f"🔗 گروه متصل: {chat.title if chat else chat_id} (<code>{chat_id}</code>)")


# --------------------------------------------------------------------------- #
# Disabled modules
# --------------------------------------------------------------------------- #
@command("خاموش‌کردن", "غیرفعال کردن", role="admin", permission="manage",
         category="tools", description="غیرفعال کردن یک بخش", usage="خاموش‌کردن بازی")
async def cmd_disable(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    key = disabled_service.resolve_key(ctx.arg_text)
    if key is None:
        from ..services.disabled import DISABLEABLE

        await ctx.reply("⚠️ بخش نامعتبر. بخش‌های قابل غیرفعال‌سازی:\n"
                        + "\n".join(f"• {label}" for label in DISABLEABLE.values()))
        return
    ok = await disabled_service.disable(ctx.session, ctx.chat_id, key)
    await ctx.reply(f"🔴 «{disabled_service.DISABLEABLE[key]}» غیرفعال شد."
                    if ok else "ℹ️ این بخش از قبل غیرفعال بود.")


@command("روشن‌کردن", "فعال کردن", role="admin", permission="manage",
         category="tools", description="فعال کردن یک بخش")
async def cmd_enable(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    key = disabled_service.resolve_key(ctx.arg_text)
    if key is None:
        await ctx.reply("⚠️ بخش نامعتبر است.")
        return
    ok = await disabled_service.enable(ctx.session, ctx.chat_id, key)
    await ctx.reply(f"🟢 «{disabled_service.DISABLEABLE[key]}» فعال شد."
                    if ok else "ℹ️ این بخش از قبل فعال بود.")


@command("بخش‌های غیرفعال", role="member", category="tools",
         description="نمایش بخش‌های غیرفعال گروه")
async def cmd_disabled_list(ctx: CommandContext) -> None:
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    await ctx.reply(disabled_service.disabled_text(settings_obj.disabled_commands or []))


# --------------------------------------------------------------------------- #
# Help / panel entry points
# --------------------------------------------------------------------------- #
HELP_TOPICS: dict[str, str] = {
    "مدیریت": "mod", "قفلها": "locks", "قفل‌ها": "locks", "فیلترها": "filters",
    "خوشامد": "welcome", "سرگرمی": "fun", "بازی": "fun", "ارز": "market",
    "قیمت": "market", "قوانین": "rules", "آمار": "stats", "پیامها": "messages",
    "پیام‌ها": "messages", "گزارش": "report", "فدراسیون": "federation",
    "اعضا": "members", "یادداشتها": "notes", "یادداشت‌ها": "notes",
    "مدیران": "staff", "ابزارها": "tools", "مالک": "owner", "مالک ربات": "owner",
}


def _topic_of(text: str) -> str | None:
    return HELP_TOPICS.get(normalize_text(text or "", mode="command").strip())


@command("راهنما", "کمک", "help", "راهنمایی", role="member", category="general",
         description="نمایش راهنمای ربات", usage="راهنما مدیریت",
         allow_in_private=True)
async def cmd_help(ctx: CommandContext) -> None:
    if ctx.arg_text.strip():
        topic = _topic_of(ctx.arg_text)
        if topic:
            await ctx.reply(help_text(topic))
            return

    lines = [f"📚 <b>راهنمای {BOT_NAME}</b>", "",
             "دستورها به فارسی و به صورت متن ساده هستند.",
             "برای اجرا روی یک کاربر، روی پیام او ریپلای کنید.", "",
             "• <code>راهنما مدیریت</code>",
             "• <code>راهنما قفل‌ها</code>",
             "• <code>راهنما فیلترها</code>",
             "• <code>راهنما خوشامد</code>",
             "• <code>راهنما سرگرمی</code>",
             "• <code>راهنما ارز</code>",
             "• <code>راهنما ابزارها</code> • <code>راهنما مالک</code> (ویژه مالک ربات)", "",
             "🎛 برای پنل گرافیکی: <code>پنل</code> یا <code>تنظیمات</code>"]
    await ctx.reply("\n".join(lines))


@command("پنل", "منو", "تنظیمات", "مدیریت", role="member", category="general",
         description="نمایش پنل مدیریت", allow_in_private=True)
async def cmd_panel(ctx: CommandContext) -> None:
    from .panel import send_panel

    await send_panel(ctx.bot, ctx.chat_id, ctx.user_id, ctx.session,
                     message=ctx.message, source_chat_id=ctx.chat_id)
    await ctx.delete_invocation()


_CHAT_TYPE_FA = {
    "private": "چت خصوصی",
    "group": "گروه معمولی",
    "supergroup": "سوپرگروه",
    "channel": "کانال",
}


async def _describe_chat(ctx: CommandContext, chat_id: int | str) -> bool:
    """Print a full description of ``chat_id``.  False when Telegram refuses."""
    from ..core.normalization import normalize_digits

    try:
        chat = await ctx.bot.get_chat(chat_id)
    except Exception:  # noqa: BLE001 - Telegram rejects chats we cannot see
        return False

    kind = _CHAT_TYPE_FA.get(chat.type, chat.type or "—")
    title = (chat.title or chat.full_name or chat.first_name or "—").strip()

    lines_out = [
        "💬 <b>اطلاعات چت</b>",
        "",
        f"📌 نوع: <b>{kind}</b>",
        f"📝 عنوان: {title}",
        f"🔢 شناسه: <code>{chat.id}</code>",
    ]

    if chat.username:
        lines_out.append("🌐 دسترسی: <b>عمومی</b>")
        lines_out.append(f"🏷 نام کاربری: @{chat.username}")
    else:
        lines_out.append("🌐 دسترسی: <b>خصوصی</b> (نام کاربری ندارد)")

    if chat.type in {"group", "supergroup", "channel"}:
        try:
            count = await ctx.bot.get_chat_member_count(chat.id)
            label = "👥 تعداد دنبال‌کنندگان" if chat.type == "channel" else "👥 تعداد اعضا"
            lines_out.append(f"{label}: {normalize_digits(str(count))}")
        except Exception:  # noqa: BLE001 - member count needs membership
            lines_out.append("👥 تعداد اعضا: در دسترس نیست (ربات عضو این چت نیست)")

    if chat.description:
        desc = chat.description.strip()
        if len(desc) > 200:
            desc = desc[:200] + "…"
        lines_out.append(f"📋 توضیحات: {desc}")

    await ctx.reply("\n".join(lines_out))
    return True


@command("آیدی گروه", "شناسه گروه", "ایدی گروه", "اطلاعات گروه", "اطلاعات کانال",
         role="member", category="general",
         description="نمایش شناسه و اطلاعات گروه یا کانال",
         usage="آیدی گروه [نام کاربری یا شناسه]")
async def cmd_chat_id(ctx: CommandContext) -> None:
    """Chat information - for the current chat or any public one by username.

    Telegram only answers ``getChat`` for chats the bot belongs to, or for
    public chats addressed by username; private chats we are not in stay
    invisible, which is exactly what the fallback message says.
    """
    from ..core.normalization import normalize_digits

    ref = (ctx.arg_text or "").strip()
    if ref:
        token = normalize_digits(ref, to="ascii").lstrip("@")
        target: int | str = int(token) if token.lstrip("-").isdigit() else f"@{token}"
        if not await _describe_chat(ctx, target):
            await ctx.reply(
                "⚠️ اطلاعات این چت در دسترس نیست.\n\n"
                "علت‌های معمول:\n"
                "• ربات عضو آن نیست و چت خصوصی است؛\n"
                "• نام کاربری اشتباه یا حذف شده است.\n\n"
                "فقط چت‌های عمومی (دارای نام کاربری) را می‌توان "
                "بدون عضویت بررسی کرد.")
        return

    if not await _describe_chat(ctx, ctx.chat_id):
        await ctx.reply(f"💬 شناسه گروه: <code>{ctx.chat_id}</code>")


# --------------------------------------------------------------------------- #
# Telegram level group administration
# --------------------------------------------------------------------------- #
async def _require_bot_right(ctx: CommandContext, right: str, label_fa: str) -> bool:
    """Make sure the bot itself holds a right, and say exactly what is missing."""
    from ..services.permissions import bot_has_right

    if await bot_has_right(ctx.bot, ctx.chat_id, right):
        return True
    await ctx.reply(
        f"⚠️ ربات برای این کار باید دسترسی {label_fa} را داشته باشد.\n"
        f"مسیر: تنظیمات گروه ← مدیران ← «{BOT_NAME}» ← فعال کردن {label_fa}")
    return False



DEFAULT_PROMOTE_RIGHTS = {
    "can_manage_chat": True,
    "can_delete_messages": True,
    "can_restrict_members": True,
    "can_invite_users": True,
    "can_pin_messages": True,
    "can_manage_video_chats": True,
    "can_change_info": False,
    "can_promote_members": False,
}

DEMOTE_RIGHTS = {
    "can_manage_chat": False,
    "can_delete_messages": False,
    "can_manage_video_chats": False,
    "can_restrict_members": False,
    "can_promote_members": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_post_messages": False,
    "can_edit_messages": False,
    "can_pin_messages": False,
    "can_manage_topics": False,
    "can_post_stories": False,
    "can_edit_stories": False,
    "can_delete_stories": False,
}


@command("ارتقا", "ادمین کردن", "مدیر کردن", "ترفیع", "اضافه کردن ادمین",
         role="admin", permission="promote", category="staff",
         description="ارتقای کاربر به مدیر گروه در تلگرام",
         usage="ارتقا (با ریپلای)")
async def cmd_promote(ctx: CommandContext) -> None:
    from ..core.errors import safe_call
    from ..core.telegram_extra import PromoteChatMemberWithTitle

    if not await require(ctx, role="admin", permission="promote"):
        return
    if not await _require_bot_right(ctx, "can_promote_members",
                                    "«افزودن مدیر جدید» (Add new admins)"):
        return
    target = await ctx.target()
    if target is None or not await guard_target(ctx, target, allow_admin=True):
        return
    rights = dict(DEFAULT_PROMOTE_RIGHTS)
    if "کامل" in normalize_text(ctx.arg_text, mode="command"):
        rights.update({"can_change_info": True, "can_promote_members": True})
    ok = await safe_call(
        lambda: ctx.bot(PromoteChatMemberWithTitle(chat_id=ctx.chat_id,
                                                   user_id=target.user_id, **rights)),
        default=None, context="promote")
    if not ok:
        await ctx.reply("⚠️ ارتقا انجام نشد. بررسی کنید ربات دسترسی «افزودن مدیر جدید» "
                        "را داشته باشد.")
        return
    invalidate_member(ctx.chat_id, target.user_id)
    await ctx.reply(f"⬆️ {user_html(target.user_id, target.full_name)} "
                    f"در تنظیمات تلگرام به مدیر گروه ارتقا یافت.\n"
                    f"{ADMIN_MANAGE_HINT}",
                    reply_markup=undo_keyboard("demote", target.user_id))


@command("تنزل", "برکناری ادمین", "حذف ادمین", "عدم ادمین", "عزل ادمین",
         "تنزل مدیر", "عزل مدیر تلگرام", "حذف مدیر تلگرام", "برکناری مدیر تلگرام",
         role="admin", permission="promote", category="staff",
         description="برکناری مدیر گروه در تلگرام", usage="تنزل (با ریپلای)")
async def cmd_demote(ctx: CommandContext) -> None:
    from ..core.errors import safe_call
    from ..core.telegram_extra import call_api_raw

    if not await require(ctx, role="admin", permission="promote"):
        return
    if not await _require_bot_right(ctx, "can_promote_members",
                                    "«افزودن مدیر جدید» (Add new admins)"):
        return
    target = await ctx.target()
    if target is None or not await guard_target(ctx, target):
        return
    # Every right is sent explicitly as False, so the member really loses all
    # of them (aiogram would silently drop falsy fields from the request).
    ok = await safe_call(
        lambda: call_api_raw(ctx.bot, "promoteChatMember",
                             {"chat_id": ctx.chat_id, "user_id": target.user_id,
                              **DEMOTE_RIGHTS}),
        default=None, context="demote")
    if not ok:
        await ctx.reply("⚠️ برکناری انجام نشد. ربات باید خودش مدیر و دارای دسترسی "
                        "«افزودن مدیر جدید» باشد.")
        return
    invalidate_member(ctx.chat_id, target.user_id)
    await ctx.reply(f"⬇️ {user_html(target.user_id, target.full_name)} "
                    f"از مدیریت گروه برکنار شد.")


BOT_RIGHT_LABELS = (
    ("can_manage_tags", "مدیریت تگ اعضا (لازم برای درج تگ در لیست اعضا)"),
    ("can_promote_members", "افزودن مدیر جدید (لازم برای ارتقا/تنزل و عنوان مدیر)"),
    ("can_restrict_members", "محدود کردن اعضا (بن/سکوت)"),
    ("can_delete_messages", "حذف پیام"),
    ("can_invite_users", "دعوت کاربر با لینک"),
    ("can_pin_messages", "سنجاق پیام"),
    ("can_change_info", "تغییر اطلاعات گروه"),
    ("can_manage_video_chats", "مدیریت چت صوتی/تصویری"),
)


@command("دسترسی ربات", "دسترسی‌های ربات", "مجوزهای ربات", "دسترسیها",
         role="admin", category="tools",
         description="نمایش دسترسی‌های مدیریتی ربات در این گروه",
         usage="دسترسی ربات")
async def cmd_bot_rights(ctx: CommandContext) -> None:
    from ..services.permissions import fetch_chat_member

    member = await fetch_chat_member(ctx.bot, ctx.chat_id, ctx.bot.id, refresh=True)
    if member is None:
        await ctx.reply("⚠️ وضعیت ربات در این گروه قابل تشخیص نیست. "
                        "مطمئن شوید ربات عضو گروه است.")
        return
    if member.status in {"left", "kicked"}:
        await ctx.reply("⚠️ ربات عضو این گروه نیست؛ ابتدا آن را به گروه اضافه کنید.")
        return
    lines = ["🔑 <b>دسترسی‌های ربات در این گروه</b>", ""]
    if member.status == "creator":
        lines.append("👑 ربات مالک گروه است؛ همهٔ دسترسی‌ها را دارد.")
    elif member.status == "administrator":
        for flag, label in BOT_RIGHT_LABELS:
            mark = "✅" if getattr(member, flag, False) else "❌"
            lines.append(f"{mark} {label}")
    else:
        lines.append("⚠️ ربات در این گروه مدیر نیست؛ ابتدا آن را مدیر کنید.")
    lines.append("")
    lines.append("مسیر تغییر: تنظیمات گروه ← مدیران ← انتخاب ربات")
    await ctx.reply("\n".join(lines))


ADMIN_MANAGE_HINT = ("💡 برای عزل: <code>تنزل</code> (با ریپلای)\n"
                    "برای ویرایش دسترسی‌ها: <code>دسترسی مدیر</code> (با ریپلای)")


@command("دسترسی مدیر", "ویرایش دسترسی مدیر", "سطح دسترسی مدیر", "دسترسی‌های مدیر",
         "دسترسیهای مدیر", "تغییر دسترسی مدیر",
         role="admin", permission="promote", category="staff",
         description="ویرایش دسترسی‌های یک مدیر گروه در تلگرام",
         usage="دسترسی مدیر (با ریپلای)")
async def cmd_admin_permissions(ctx: CommandContext) -> None:
    from ..keyboards.menus import ADMIN_RIGHTS, admin_rights_keyboard, admin_rights_lines
    from ..services.permissions import fetch_chat_member

    if not await require(ctx, role="admin", permission="promote"):
        return
    if not await _require_bot_right(ctx, "can_promote_members",
                                    "«افزودن مدیر جدید» (Add new admins)"):
        return
    target = await ctx.target()
    if target is None or not await guard_target(ctx, target, allow_admin=True):
        return
    member = await fetch_chat_member(ctx.bot, ctx.chat_id, target.user_id, refresh=True)
    if member is None:
        await ctx.reply("⚠️ وضعیت این کاربر در گروه قابل تشخیص نیست.")
        return
    if member.status == "creator":
        await ctx.reply("👑 دسترسی‌های «مالک گروه» در تلگرام قابل ویرایش نیست.")
        return
    if member.status != "administrator":
        await ctx.reply("ℹ️ این کاربر مدیر گروه نیست.\n"
                        "برای ارتقا، روی پیام او ریپلای کنید و بنویسید <code>ارتقا</code>.")
        return
    if not getattr(member, "can_be_edited", False):
        await ctx.reply(
            "⚠️ تلگرام فقط اجازهٔ ویرایش مدیرانی را می‌دهد که توسط <b>خود ربات</b> "
            "منصوب شده باشند.\n"
            "یک بار روی پیام او ریپلای کنید و بنویسید <code>ارتقا</code>؛ "
            "بعد از آن دسترسی‌هایش قابل ویرایش خواهد بود.")
        return
    rights = {key: bool(getattr(member, key, False)) for key, _label, _default in ADMIN_RIGHTS}
    text = "\n".join(admin_rights_lines(target.full_name, target.user_id, rights))
    await ctx.reply(text, reply_markup=admin_rights_keyboard(target.user_id, rights))


@command("عزل", "برکناری", role="admin", permission="promote", category="staff",
         description="برکناری مدیر تلگرام (معادل «تنزل» - بدون آرگومان)")
async def cmd_demote_plain(ctx: CommandContext) -> None:
    await cmd_demote(ctx)


@command("تنظیم عنوان", "تغییر عنوان", "نام گروه", role="admin", permission="change_info",
         category="tools", description="تغییر نام گروه در تلگرام",
         usage="تنظیم عنوان نام جدید گروه")
async def cmd_set_title(ctx: CommandContext) -> None:
    from ..core.errors import safe_call

    if not await require(ctx, role="admin", permission="change_info"):
        return
    title = (ctx.arg_text or "").strip()
    if not title:
        await ctx.reply("⚠️ نام جدید را بنویسید. مثال: <code>تنظیم عنوان گروه دوستان</code>")
        return
    ok = await safe_call(lambda: ctx.bot.set_chat_title(chat_id=ctx.chat_id, title=title[:128]),
                         default=None, context="set_title")
    if not ok:
        await ctx.reply("⚠️ تغییر نام انجام نشد (ربات باید دسترسی تغییر اطلاعات گروه را داشته باشد).")
        return
    await ctx.reply(f"✏️ نام گروه به «{html.escape(title[:128])}» تغییر یافت.")


@command("تنظیم توضیحات", "تغییر توضیحات", "توضیحات گروه", role="admin",
         permission="change_info", category="tools",
         description="تغییر توضیحات گروه در تلگرام", usage="تنظیم توضیحات متن توضیحات")
async def cmd_set_description(ctx: CommandContext) -> None:
    from ..core.errors import safe_call

    if not await require(ctx, role="admin", permission="change_info"):
        return
    description = (ctx.arg_text or "").strip()
    if not description:
        await ctx.reply("⚠️ متن توضیحات را بنویسید.")
        return
    ok = await safe_call(
        lambda: ctx.bot.set_chat_description(chat_id=ctx.chat_id, description=description[:255]),
        default=None, context="set_description")
    if not ok:
        await ctx.reply("⚠️ ثبت توضیحات انجام نشد (ربات باید دسترسی تغییر اطلاعات گروه را داشته باشد).")
        return
    await ctx.reply("📝 توضیحات گروه در تلگرام به‌روزرسانی شد.")


# --------------------------------------------------------------------------- #
# Invite links
# --------------------------------------------------------------------------- #
@command("ساخت لینک", "لینک جدید", "لینک موقت", "لینک دعوت", "ایجاد لینک",
         role="admin", permission="invite", category="tools",
         description="ساخت لینک دعوت موقت گروه (پیش‌فرض: ۶۰ دقیقه)",
         usage="ساخت لینک [مدت؛ مثل ۱ ساعت]")
async def cmd_create_link(ctx: CommandContext) -> None:
    from datetime import datetime, timedelta
    from math import ceil

    if not await require(ctx, role="admin", permission="invite"):
        return
    if not await _require_bot_right(ctx, "can_invite_users", "دعوت کاربران با لینک"):
        return
    # No duration argument means a one-hour invite. Parse explicit durations
    # as seconds so «۱ ساعت» is exactly 60 minutes (not 6 hours).
    duration_seconds = 60 * 60
    limit = 1
    raw = normalize_text(ctx.arg_text, mode="command")
    if "نامحدود" in raw or "دائم" in raw:
        duration_seconds, limit = 0, 0
    elif raw:
        parsed = parse_duration(raw)
        if parsed is not None:
            duration_seconds = parsed
    kwargs: dict[str, object] = {
        "chat_id": ctx.chat_id,
        "name": f"Armando-{ctx.user_id}",
    }
    if duration_seconds:
        kwargs["expire_date"] = int((datetime.utcnow() + timedelta(seconds=duration_seconds)).timestamp())
    if limit:
        kwargs["member_limit"] = limit
    link = await safe_call(lambda: ctx.bot.create_chat_invite_link(**kwargs),
                           default=None, context="invite_link")
    if link is None:
        await ctx.reply("⚠️ ساخت لینک انجام نشد. ربات باید دسترسی «دعوت کاربران با لینک» "
                        "را داشته باشد.")
        return
    details = []
    if duration_seconds:
        minutes = ceil(duration_seconds / 60)
        details.append(f"⏳ اعتبار: {to_persian_digits(str(minutes))} دقیقه")
    if limit:
        details.append(f"👤 تعداد مجاز: {to_persian_digits(str(limit))} نفر")
    extra = ("\n" + " • ".join(details)) if details else ""
    await ctx.reply(f"🔗 لینک دعوت ساخته شد:{extra}\n{link.invite_link}")


@command("لینک گروه", "لینک", "دریافت لینک", role="admin", permission="invite",
         category="tools", description="دریافت لینک دعوت فعلی گروه")
async def cmd_export_link(ctx: CommandContext) -> None:
    from ..core.errors import safe_call

    if not await require(ctx, role="admin", permission="invite"):
        return
    link = await safe_call(lambda: ctx.bot.export_chat_invite_link(chat_id=ctx.chat_id),
                           default=None, context="export_link")
    if not link:
        await ctx.reply("⚠️ دریافت لینک انجام نشد. ربات باید دسترسی «دعوت کاربران با لینک» "
                        "را داشته باشد.")
        return
    await ctx.reply(f"🔗 لینک گروه:\n{link}")


def _register_help_topics() -> None:
    """Register ``راهنما مدیریت`` / ``کمک قفل‌ها`` ... as real commands."""
    from ..keyboards.menus import BOT_NAME, help_text
    from .registry import Command, registry

    for word, topic in HELP_TOPICS.items():
        def make(topic_key: str):
            async def handler(ctx: CommandContext) -> None:
                await ctx.reply(help_text(topic_key))

            return handler

        registry.register(Command(
            phrases=(f"راهنما {word}", f"کمک {word}"),
            handler=make(topic),
            role="member",
            category="general",
            description=f"راهنمای {word}",
            allow_in_private=True,
        ))


_register_help_topics()
