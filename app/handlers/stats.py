"""Statistics commands: آمار، فعال‌ترین‌ها، غیرفعال‌ها، روند گروه."""

from __future__ import annotations

import logging

from ..core.normalization import normalize_digits
from ..services import disabled as disabled_service
from ..services import stats as stats_service
from .common import CommandContext, require
from .registry import command

logger = logging.getLogger("armando.handlers.stats")


def _days(ctx: CommandContext, default: int = 7) -> int:
    raw = normalize_digits(ctx.arg_text or "", to="ascii")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return default
    value = int(digits)
    return max(1, min(value, 365))


@command("آمار", "امار", "آمار گروه", role="member", category="stats",
         description="نمایش آمار گروه")
async def cmd_stats(ctx: CommandContext) -> None:
    if await disabled_service.is_disabled(ctx.session, ctx.chat_id, "stats"):
        return
    report = await stats_service.group_report(ctx.session, ctx.chat_id)
    await ctx.reply(stats_service.format_group_report(report))


@command("فعال‌ترین‌ها", "فعالترینها", "فعال ترین ها", "برترین اعضا",
         role="member", category="stats", description="فعال‌ترین کاربران",
         usage="فعال‌ترین‌ها ۳۰")
async def cmd_top_users(ctx: CommandContext) -> None:
    days = _days(ctx, 7)
    limit = 10 if days <= 7 else 20
    await ctx.reply(await stats_service.top_users_text(ctx.session, ctx.chat_id,
                                                       days=days, limit=limit))


@command("غیرفعال‌ها", "غیرفعالها", "اعضای غیرفعال", role="moderator",
         category="stats", description="اعضای غیرفعال بر اساس داده‌های ربات",
         usage="غیرفعال‌ها ۳۰")
async def cmd_inactive(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator"):
        return
    days = _days(ctx, 30)
    await ctx.reply(await stats_service.inactive_users_text(ctx.session, ctx.chat_id,
                                                            days=days, limit=30))


@command("روند گروه", "نمودار", "رشد اعضا", role="admin", permission="manage",
         category="stats", description="نمودار روند اعضای گروه")
async def cmd_trend(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    from aiogram.types import BufferedInputFile

    chart = await stats_service.growth_chart(ctx.session, ctx.chat_id, days=30)
    if chart is None:
        report = await stats_service.group_report(ctx.session, ctx.chat_id)
        await ctx.reply("📈 هنوز داده کافی برای رسم نمودار وجود ندارد "
                        "(حداقل دو روز ثبت نیاز است).\n\n"
                        + stats_service.format_group_report(report))
        return
    document = BufferedInputFile(chart, filename="trend.png")
    await ctx.reply("📈 روند اعضای ۳۰ روز اخیر", reply_to=False)
    from ..core.errors import safe_call

    await safe_call(lambda: ctx.bot.send_photo(chat_id=ctx.chat_id, photo=document,
                                               caption="📈 روند اعضای گروه"),
                    context="send_chart")


@command("آمار من", "فعالیت من", role="member", category="stats",
         description="نمایش فعالیت شخصی شما")
async def cmd_my_stats(ctx: CommandContext) -> None:
    from sqlalchemy import select

    from ..core.normalization import to_persian_digits
    from ..db.models import ChatMemberState
    from ..core.timeutils import persian_relative

    result = await ctx.session.execute(
        select(ChatMemberState).where(ChatMemberState.chat_id == ctx.chat_id,
                                      ChatMemberState.user_id == ctx.user_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        await ctx.reply("ℹ️ هنوز فعالیتی از شما ثبت نشده است.")
        return
    lines = [
        "📊 <b>فعالیت شما</b>",
        "",
        f"📨 تعداد پیام: {to_persian_digits(str(state.message_count or 0))}",
        f"🖼 رسانه: {to_persian_digits(str(state.media_count or 0))}",
        f"⌨️ دستور: {to_persian_digits(str(state.command_count or 0))}",
        f"⚠️ اخطارها: {to_persian_digits(str(state.warn_count or 0))}",
    ]
    if state.last_message_at:
        lines.append(f"🕒 آخرین فعالیت: {persian_relative(state.last_message_at)}")
    await ctx.reply("\n".join(lines))
