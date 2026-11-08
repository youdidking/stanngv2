"""Entertainment, market prices and date/time commands (Persian APIs)."""

from __future__ import annotations

import logging


from ..core.timeutils import (
    calendar_details_text,
    date_details_text,
    tehran_now,
    time_details_text,
)
from ..services import disabled as disabled_service
from ..services import entertainment, market
from ..services.market import (
    GOLD,
    crypto_list_text,
    currency_list_text,
    get_bourse,
    get_crypto,
    get_currency,
    get_gold,
    overview_text,
    quote_text,
    resolve_crypto,
    resolve_currency,
    resolve_gold,
)
from .common import CommandContext
from .registry import command

logger = logging.getLogger("armando.handlers.fun")


async def _module_enabled(ctx: CommandContext, key: str) -> bool:
    return not await disabled_service.is_disabled(ctx.session, ctx.chat_id, key)


# --------------------------------------------------------------------------- #
# Entertainment
# --------------------------------------------------------------------------- #
@command("جوک", "یه جوک", "بگو جوک", role="member", category="fun",
         description="ارسال یک جوک فارسی")
async def cmd_joke(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "joke"):
        return
    await ctx.reply(entertainment.random_joke())


@command("فال حافظ", "فال", "حافظ", role="member", category="fun",
         description="فال حافظ با تعبیر")
async def cmd_fal(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "fortune"):
        return
    await ctx.reply(entertainment.fal_text())


@command("شیر یا خط", "سکه", role="member", category="fun", description="انداختن سکه")
async def cmd_coin(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "games"):
        return
    await ctx.reply(entertainment.flip_coin())


@command("تاس", role="member", category="fun", description="انداختن تاس")
async def cmd_dice(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "games"):
        return
    await entertainment.send_dice(ctx.bot, ctx.chat_id, "🎲",
                                  reply_to_message_id=ctx.message.message_id)


@command("عدد تصادفی", "عدد", role="member", category="fun",
         description="تولید عدد تصادفی", usage="عدد تصادفی ۱ ۱۰۰")
async def cmd_random(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "games"):
        return
    parsed = entertainment.parse_range(ctx.arg_text)
    if parsed:
        low, high = parsed
    else:
        low, high = 1, 100
    await ctx.reply(entertainment.random_number(low, high))


@command("اسلات", "ماشین اسلات", role="member", category="fun", description="بازی اسلات")
async def cmd_slot(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "games"):
        return
    text, _ = entertainment.slot_machine()
    await ctx.reply(text)


@command("بازی", "حدس عدد", "شروع بازی", role="member", category="fun",
         description="شروع بازی حدس عدد")
async def cmd_game(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "games"):
        return
    from ..core.normalization import to_persian_digits

    high_raw = ctx.arg_text.strip()
    high = 100
    if high_raw:
        digits = "".join(ch for ch in high_raw if ch.isdigit() or ch in "۰۱۲۳۴۵۶۷۸۹")
        if digits:
            from ..core.normalization import normalize_digits

            high = int(normalize_digits(digits, to="ascii"))
    entertainment.guess_game.start(ctx.chat_id, ctx.user_id, high)
    await ctx.reply(f"🎮 بازی شروع شد! عددی بین ۱ تا {to_persian_digits(str(high))} حدس بزن.")


# --------------------------------------------------------------------------- #
# Market
# --------------------------------------------------------------------------- #
@command("ارز", "نرخ ارز", "قیمت ارز", "نرخ", role="member", category="market",
         description="نمایش نرخ لحظه‌ای ارز، طلا و ارز دیجیتال",
         usage="ارز | قیمت دلار | قیمت بیت‌کوین")
async def cmd_currency_overview(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "market"):
        return
    await ctx.reply(await overview_text())


@command("قیمت", "نرخ لحظه‌ای", role="member", category="market",
         description="قیمت ارز، ارز دیجیتال یا طلا", usage="قیمت دلار / قیمت btc / قیمت سکه")
async def cmd_price(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "market"):
        return
    target = ctx.arg_text.strip()
    if not target:
        await ctx.reply("💱 چه چیزی مد نظرتان است؟\n"
                        "مثال: <code>قیمت دلار</code> • <code>قیمت بیت‌کوین</code> • <code>قیمت سکه</code>")
        return
    crypto_key = resolve_crypto(target)
    if crypto_key:
        quote = await get_crypto(crypto_key)
        if quote:
            await ctx.reply(quote_text(quote))
            return
    currency_key = resolve_currency(target)
    if currency_key:
        quote = await get_currency(currency_key)
        if quote:
            await ctx.reply(quote_text(quote))
            return
    gold_key = resolve_gold(target)
    if gold_key:
        quote = await get_gold(gold_key)
        if quote:
            await ctx.reply(quote_text(quote))
            return
    await ctx.reply("ℹ️ مورد درخواستی پیدا نشد یا منبع در دسترس نیست.\n"
                    "مثال‌ها: <code>قیمت دلار</code> • <code>قیمت یورو</code> • "
                    "<code>قیمت تتر</code> • <code>قیمت سکه</code>")


@command("طلا", "قیمت طلا", "طلای ۱۸ عیار", role="member", category="market",
         description="قیمت طلا و سکه")
async def cmd_gold(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "market"):
        return
    key = resolve_gold(ctx.arg_text or "coin_emami") or "coin_emami"
    lines = ["🥇 <b>بازار طلا و سکه</b>", ""]
    keys = [key] if ctx.arg_text.strip() else ["coin_emami", "coin_bahar", "gram18", "mesghal", "ounce"]
    for item in keys:
        quote = await get_gold(item)
        if quote:
            lines.append(f"• {quote.label}: {quote.price_text}  {quote.change_text}")
    if len(lines) == 2:
        await ctx.reply("⚠️ در حال حاضر امکان دریافت قیمت طلا وجود ندارد.")
        return
    await ctx.reply("\n".join(lines))


@command("بورس", "شاخص بورس", "شاخص کل", role="member", category="market",
         description="شاخص کل بورس")
async def cmd_bourse(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "market"):
        return
    quote = await get_bourse()
    if quote is None:
        await ctx.reply("⚠️ در حال حاضر امکان دریافت شاخص بورس وجود ندارد.")
        return
    await ctx.reply(quote_text(quote, title="📈 شاخص کل بورس"))


@command("ارز دیجیتال", "کریپتو", "رمزارز", "رمز ارز", role="member",
         category="market", description="نمایش قیمت ارزهای دیجیتال")
async def cmd_crypto_list(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "market"):
        return
    if ctx.arg_text.strip():
        await cmd_price(ctx)
        return
    await ctx.reply(crypto_list_text())


@command("لیست ارزها", "ارزها", role="member", category="market",
         description="نمایش ارزهای پشتیبانی‌شده")
async def cmd_currency_list(ctx: CommandContext) -> None:
    await ctx.reply(currency_list_text())


# --------------------------------------------------------------------------- #
# Date & time
# --------------------------------------------------------------------------- #
@command("تاریخ", "تاریخ امروز", "امروز", role="member", category="market",
         description="نمایش جزئیات تاریخ شمسی و میلادی امروز")
async def cmd_date(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "time"):
        return
    await ctx.reply(date_details_text(tehran_now()))


@command("ساعت", "زمان", "الان", role="member", category="market",
         description="نمایش ساعت دقیق به وقت منطقهٔ زمانی تنظیم‌شده")
async def cmd_time(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "time"):
        return
    await ctx.reply(time_details_text(tehran_now()))


async def _remote_time_text() -> str:
    """Optional remote clock check (fails silently)."""
    from ..config import settings

    if not settings.enable_remote_time_sync:
        return ""
    try:
        data = await market._get_json(settings.time_api_url)
        if not isinstance(data, dict):
            return ""
        date_time = data.get("dateTime")
        if not date_time:
            return ""
        return "timeapi.io"
    except Exception:  # noqa: BLE001
        return ""


@command("تقویم", "تاریخ و ساعت", role="member", category="market",
         description="نمایش تاریخ و ساعت کامل")
async def cmd_calendar(ctx: CommandContext) -> None:
    if not await _module_enabled(ctx, "time"):
        return
    await ctx.reply(calendar_details_text(tehran_now()))
