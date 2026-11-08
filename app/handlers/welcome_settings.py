"""Welcome / goodbye / rules / captcha / night-mode / anti-spam settings."""

from __future__ import annotations

import logging

from ..core.normalization import normalize_digits, normalize_text, to_persian_digits
from ..core.timeutils import parse_hhmm
from ..services import nightmode
from ..services.chat_state import get_settings, invalidate_settings
from ..services.locks import set_lock
from ..services.moderation import PUNISHMENT_OPTIONS_FA
from .common import CommandContext, require
from .registry import command

logger = logging.getLogger("armando.handlers.welcome")

ACTION_MAP_FA = {
    "حذف": "delete", "حذف پیام": "delete",
    "اخطار": "warn", "سکوت": "mute", "سکوت موقت": "temp_mute",
    "اخراج": "kick", "بن": "ban", "بن موقت": "temp_ban",
    "حذف و اخطار": "delete_warn", "حذف + اخطار": "delete_warn",
    "حذف و سکوت": "delete_mute", "حذف + سکوت": "delete_mute",
    "حذف و بن": "delete_ban", "حذف + بن": "delete_ban",
    "هیچ": "none", "بدون اقدام": "none", "فقط هشدار": "none",
}


def _parse_int(text: str, default: int | None = None) -> int | None:
    raw = normalize_digits(text or "", to="ascii")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return default
    try:
        return int(digits)
    except ValueError:
        return default


def _media_of(message) -> tuple[str, str | None, str]:
    if message is None:
        return "text", None, ""
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption or ""
    if message.video:
        return "video", message.video.file_id, message.caption or ""
    if message.animation:
        return "animation", message.animation.file_id, message.caption or ""
    if message.document:
        return "document", message.document.file_id, message.caption or ""
    if message.audio:
        return "audio", message.audio.file_id, message.caption or ""
    if message.voice:
        return "voice", message.voice.file_id, ""
    return "text", None, message.text or message.caption or ""


# --------------------------------------------------------------------------- #
# Welcome / goodbye
# --------------------------------------------------------------------------- #
@command("تنظیم خوشامد", "تنظیم پیام خوشامد", "خوشامد", role="admin", permission="manage",
         category="welcome", description="تنظیم پیام خوشامد",
         usage="تنظیم خوشامد سلام {first_name} عزیز")
async def cmd_set_welcome(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    body = ctx.arg_text.strip()
    reply = ctx.message.reply_to_message
    if not body and reply is not None:
        content_type, file_id, caption = _media_of(reply)
        if content_type != "text":
            settings_obj.welcome_media = {"type": content_type, "file_id": file_id,
                                          "caption": caption}
            settings_obj.welcome_enabled = True
            invalidate_settings(ctx.chat_id)
            await ctx.reply("🌸 رسانه پیام خوشامد ذخیره شد.")
            return
    if not body:
        await ctx.reply("⚠️ متن خوشامد را بنویسید یا روی یک رسانه ریپلای کنید.\n"
                        "متغیرها: {first_name} {last_name} {username} {chat_name} {date} {time}")
        return
    settings_obj.welcome_text = body
    settings_obj.welcome_media = None
    settings_obj.welcome_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🌸 پیام خوشامد ذخیره شد.")


@command("تنظیم خداحافظی", "خداحافظی", role="admin", permission="manage",
         category="welcome", description="تنظیم پیام خداحافظی")
async def cmd_set_goodbye(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    body = ctx.arg_text.strip()
    if not body:
        await ctx.reply("⚠️ متن خداحافظی را بنویسید.\nمتغیرها: {first_name} {chat_name}")
        return
    settings_obj.goodbye_text = body
    settings_obj.goodbye_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply("👋 پیام خداحافظی ذخیره شد.")


@command("روشن‌کردن خوشامد", "فعال کردن خوشامد", "روشن کردن خوشامد", role="admin",
         permission="manage", category="welcome", description="فعال کردن پیام خوشامد")
async def cmd_welcome_on(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.welcome_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🟢 پیام خوشامد فعال شد.")


@command("خاموش‌کردن خوشامد", "غیرفعال کردن خوشامد", role="admin", permission="manage",
         category="welcome", description="غیرفعال کردن پیام خوشامد")
async def cmd_welcome_off(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.welcome_enabled = False
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🔴 پیام خوشامد غیرفعال شد.")


@command("حذف خودکار خوشامد", "زمان حذف خوشامد", role="admin", permission="manage",
         category="welcome", description="حذف خودکار پیام خوشامد پس از چند ثانیه")
async def cmd_welcome_autodelete(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    await ctx.reply("⏱ حذف جداگانهٔ خوشامد غیرفعال است؛ پیام‌های عادی ربات در گروه پس از ۳۰ ثانیه پاک می‌شوند.")


# --------------------------------------------------------------------------- #
# Service message cleanup
# --------------------------------------------------------------------------- #
SERVICE_TOGGLES = {
    "پاکسازی ورود": "clean_join",
    "پاکسازی خروج": "clean_leave",
    "پاکسازی پین": "clean_pin",
    "پاکسازی سرویسی": "clean_service",
    "پاکسازی کانال": "clean_channel_post",
    "پاکسازی ویس‌چت": "clean_voice_chat",
}


def _toggle_field(ctx: CommandContext) -> str | None:
    for phrase, field in SERVICE_TOGGLES.items():
        if normalize_text(ctx.command, mode="command").startswith(normalize_text(phrase, mode="command")):
            return field
    return None


def _register_service_toggles() -> None:
    from .registry import registry

    for phrase, field in SERVICE_TOGGLES.items():
        def make(field_name: str, label: str):
            async def handler(ctx: CommandContext) -> None:
                if not await require(ctx, role="admin", permission="manage"):
                    return
                settings_obj = await get_settings(ctx.session, ctx.chat_id)
                current = bool(getattr(settings_obj, field_name, False))
                setattr(settings_obj, field_name, not current)
                invalidate_settings(ctx.chat_id)
                await ctx.reply(f"{'🟢' if not current else '🔴'} {label} "
                                f"{'فعال' if not current else 'غیرفعال'} شد.")

            return handler

        registry.register(type(registry.commands[0])(
            phrases=(phrase,),
            handler=make(field, phrase),
            role="admin",
            permission="manage",
            category="welcome",
            description=f"تغییر وضعیت {phrase}",
        ))


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
@command("تنظیم قوانین", role="admin", permission="manage", category="rules",
         description="تنظیم متن قوانین گروه", usage="تنظیم قوانین ۱. احترام ...")
async def cmd_set_rules(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    body = ctx.arg_text.strip()
    if not body and ctx.message.reply_to_message:
        body = ctx.message.reply_to_message.text or ctx.message.reply_to_message.caption or ""
    if not body:
        await ctx.reply("⚠️ متن قوانین را بنویسید: <code>تنظیم قوانین ۱. احترام به دیگران</code>")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.rules_text = body
    settings_obj.rules_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply("📜 قوانین گروه ذخیره شد.\nبرای مشاهده: <code>قوانین</code>")


@command("قوانین", "نمایش قوانین", role="member", category="rules",
         description="نمایش قوانین گروه")
async def cmd_rules(ctx: CommandContext) -> None:
    rules = (ctx.settings.get("rules_text") or "").strip()
    if not rules:
        await ctx.reply("ℹ️ قوانینی برای این گروه تنظیم نشده است.")
        return
    await ctx.reply(f"📜 <b>قوانین {ctx.chat_title}</b>\n\n{rules}")


@command("حذف قوانین", role="admin", permission="manage", category="rules",
         description="حذف متن قوانین")
async def cmd_delete_rules(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.rules_text = ""
    invalidate_settings(ctx.chat_id)
    await ctx.reply("🗑 قوانین حذف شد.")


# --------------------------------------------------------------------------- #
# Captcha
# --------------------------------------------------------------------------- #
@command("تنظیم کپچا", "کپچا", role="admin", permission="manage", category="welcome",
         description="فعال/غیرفعال کردن کپچا")
async def cmd_captcha_toggle(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.captcha_enabled = not bool(settings_obj.captcha_enabled)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢 کپچا فعال شد.' if settings_obj.captcha_enabled else '🔴 کپچا غیرفعال شد.'}")


@command("حالت کپچا", role="admin", permission="manage", category="welcome",
         description="انتخاب نوع چالش کپچا", usage="حالت کپچا ریاضی")
async def cmd_captcha_mode(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    modes = {"دکمه": "button", "دکمهای": "button", "ساده": "button",
             "ریاضی": "math", "ایموجی": "emoji", "شکلک": "emoji"}
    mode = modes.get(normalize_text(ctx.arg_text, mode="command"))
    if mode is None:
        await ctx.reply("❌ حالت نامعتبر. گزینه‌ها: دکمه، ریاضی، ایموجی")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.captcha_mode = mode
    settings_obj.captcha_enabled = True
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ نوع چالش روی «{ctx.arg_text}» تنظیم شد.")


@command("زمان کپچا", role="admin", permission="manage", category="welcome",
         description="مهلت پاسخ‌گویی به کپچا (ثانیه)")
async def cmd_captcha_timeout(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    seconds = _parse_int(ctx.arg_text)
    if not seconds or not 30 <= seconds <= 900:
        await ctx.reply("❌ مهلت باید بین ۳۰ تا ۹۰۰ ثانیه باشد.")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.captcha_timeout = seconds
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ مهلت کپچا روی {to_persian_digits(str(seconds))} ثانیه تنظیم شد.")


@command("تلاش کپچا", role="admin", permission="manage", category="welcome",
         description="تعداد تلاش‌های مجاز کپچا")
async def cmd_captcha_attempts(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    attempts = _parse_int(ctx.arg_text)
    if not attempts or not 1 <= attempts <= 10:
        await ctx.reply("❌ تعداد تلاش باید بین ۱ تا ۱۰ باشد.")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.captcha_max_attempts = attempts
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ تعداد تلاش‌های مجاز: {to_persian_digits(str(attempts))}")


@command("اقدام کپچا", role="admin", permission="manage", category="welcome",
         description="اقدام پس از شکست در کپچا", usage="اقدام کپچا اخراج")
async def cmd_captcha_action(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    action = ACTION_MAP_FA.get(normalize_text(ctx.arg_text, mode="command"))
    if action not in {"kick", "ban", "mute", "none"}:
        await ctx.reply("❌ گزینه‌ها: اخراج، بن، سکوت، بدون اقدام")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.captcha_fail_action = action
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ اقدام پس از شکست در کپچا: {PUNISHMENT_OPTIONS_FA.get(action, action)}")


# --------------------------------------------------------------------------- #
# Night mode
# --------------------------------------------------------------------------- #
@command("حالت شب", "شبانه", role="admin", permission="manage", category="welcome",
         description="فعال/غیرفعال کردن حالت شب")
async def cmd_night_toggle(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.night_mode_enabled = not bool(settings_obj.night_mode_enabled)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(nightmode.night_status_text(settings_obj))


@command("حالت شب شروع", "شروع شب", role="admin", permission="manage",
         category="welcome", description="ساعت شروع حالت شب", usage="حالت شب شروع ۲۳:۳۰")
async def cmd_night_start(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    parsed = parse_hhmm(ctx.arg_text)
    if parsed is None:
        await ctx.reply("❌ قالب زمان نامعتبر. مثال: <code>حالت شب شروع ۲۳:۳۰</code>")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.night_start = f"{parsed[0]:02d}:{parsed[1]:02d}"
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"🌙 شروع حالت شب: {to_persian_digits(settings_obj.night_start)}")


@command("حالت شب پایان", "پایان شب", role="admin", permission="manage",
         category="welcome", description="ساعت پایان حالت شب")
async def cmd_night_end(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    parsed = parse_hhmm(ctx.arg_text)
    if parsed is None:
        await ctx.reply("❌ قالب زمان نامعتبر. مثال: <code>حالت شب پایان ۰۷:۰۰</code>")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.night_end = f"{parsed[0]:02d}:{parsed[1]:02d}"
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"🌙 پایان حالت شب: {to_persian_digits(settings_obj.night_end)}")


@command("اقدام شب", role="admin", permission="manage", category="welcome",
         description="اقدام حالت شب", usage="اقدام شب سکوت")
async def cmd_night_action(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    action = ACTION_MAP_FA.get(normalize_text(ctx.arg_text, mode="command"))
    if action not in {"mute", "delete", "kick", "ban", "none"}:
        await ctx.reply("❌ گزینه‌ها: سکوت، حذف، اخراج، بن، بدون اقدام")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.night_action = action
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ اقدام حالت شب: {PUNISHMENT_OPTIONS_FA.get(action, action)}")


# --------------------------------------------------------------------------- #
# Anti-spam / anti-raid toggles
# --------------------------------------------------------------------------- #
@command("ضداسپم", role="admin", permission="manage", category="locks",
         description="فعال/غیرفعال کردن ضداسپم")
async def cmd_antispam_toggle(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.antispam_enabled = not bool(settings_obj.antispam_enabled)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢 ضداسپم فعال شد.' if settings_obj.antispam_enabled else '🔴 ضداسپم غیرفعال شد.'}")


@command("ضدفلاود", role="admin", permission="manage", category="locks",
         description="فعال/غیرفعال یا تنظیم ضدفلاود", usage="ضدفلاود ۵ ۳")
async def cmd_antiflood(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    values = [_parse_int(token) for token in ctx.args[:2]]
    if all(v is not None for v in values) and len(values) == 2:
        count, window = values
        if not 2 <= count <= 50 or not 1 <= window <= 120:
            await ctx.reply("❌ محدوده مجاز: ۲ تا ۵۰ پیام در ۱ تا ۱۲۰ ثانیه")
            return
        settings_obj.antiflood_count = count
        settings_obj.antiflood_window = window
        settings_obj.antiflood_enabled = True
        invalidate_settings(ctx.chat_id)
        await ctx.reply(f"🌊 ضدفلاود: {to_persian_digits(str(count))} پیام در "
                        f"{to_persian_digits(str(window))} ثانیه")
        return
    settings_obj.antiflood_enabled = not bool(settings_obj.antiflood_enabled)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢 ضدفلاود فعال شد.' if settings_obj.antiflood_enabled else '🔴 ضدفلاود غیرفعال شد.'}")


@command("اقدام فلاود", role="admin", permission="manage", category="locks",
         description="اقدام هنگام فلاود")
async def cmd_flood_action(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    action = ACTION_MAP_FA.get(normalize_text(ctx.arg_text, mode="command"))
    if action is None:
        await ctx.reply("❌ گزینه‌ها: حذف، اخطار، سکوت، اخراج، بن و ترکیب‌ها")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.antiflood_action = action
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ اقدام فلاود: {PUNISHMENT_OPTIONS_FA.get(action, action)}")


@command("تکرار پیام", role="admin", permission="manage", category="locks",
         description="برخورد با پیام‌های تکراری (۰ = خاموش)")
async def cmd_repeat_limit(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    value = _parse_int(ctx.arg_text, 0)
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.antispam_same_limit = max(0, min(int(value or 0), 20))
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"🔁 تکرار پیام: {to_persian_digits(str(settings_obj.antispam_same_limit))} بار"
                    if settings_obj.antispam_same_limit else "🔴 برخورد با پیام تکراری خاموش شد.")


@command("منشن اسپم", role="admin", permission="manage", category="locks",
         description="حد مجاز منشن در هر پیام (۰ = خاموش)")
async def cmd_mention_limit(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    value = _parse_int(ctx.arg_text, 0)
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.mention_spam_limit = max(0, min(int(value or 0), 30))
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"📣 حد منشن: {to_persian_digits(str(settings_obj.mention_spam_limit))}"
                    if settings_obj.mention_spam_limit else "🔴 منشن اسپم خاموش شد.")


@command("ضد رید", "ضد Raid", "حالت ضد رید", "ضدراید", role="admin",
         permission="manage", category="locks", description="تنظیم ضد Raid",
         usage="ضد رید ۸ ۶۰")
async def cmd_antiraid(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    values = [_parse_int(token) for token in ctx.args[:2]]
    if all(v is not None for v in values) and len(values) == 2:
        threshold, window = values
        settings_obj.antiraid_threshold = max(2, min(int(threshold), 100))
        settings_obj.antiraid_window = max(10, min(int(window), 3600))
        settings_obj.antiraid_enabled = True
        invalidate_settings(ctx.chat_id)
        await ctx.reply(f"🛡 ضد Raid: {to_persian_digits(str(settings_obj.antiraid_threshold))} ورود در "
                        f"{to_persian_digits(str(settings_obj.antiraid_window))} ثانیه")
        return
    settings_obj.antiraid_enabled = not bool(settings_obj.antiraid_enabled)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢 ضد Raid فعال شد.' if settings_obj.antiraid_enabled else '🔴 ضد Raid غیرفعال شد.'}")


@command("اقدام رید", role="admin", permission="manage", category="locks",
         description="اقدام پس از تشخیص Raid")
async def cmd_raid_action(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    action = ACTION_MAP_FA.get(normalize_text(ctx.arg_text, mode="command"))
    if action not in {"kick", "ban", "mute", "none"}:
        await ctx.reply("❌ گزینه‌ها: اخراج، بن، سکوت، فقط هشدار")
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.antiraid_action = action
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"✅ اقدام ضد Raid: {PUNISHMENT_OPTIONS_FA.get(action, action)}")


@command("ضدفحاشی", role="admin", permission="manage", category="locks",
         description="فعال/غیرفعال کردن ضدفحاشی")
async def cmd_profanity_toggle(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.anti_profanity_enabled = not bool(settings_obj.anti_profanity_enabled)
    invalidate_settings(ctx.chat_id)
    await set_lock(ctx.session, ctx.chat_id, "profanity",
                   bool(settings_obj.anti_profanity_enabled),
                   action=settings_obj.anti_profanity_action, updated_by=ctx.user_id)
    await ctx.reply(f"{'🟢 ضدفحاشی فعال شد.' if settings_obj.anti_profanity_enabled else '🔴 ضدفحاشی غیرفعال شد.'}")


@command("ضدپورن", role="admin", permission="manage", category="locks",
         description="فعال/غیرفعال کردن ضدپورن")
async def cmd_porn_toggle(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.anti_porn_enabled = not bool(settings_obj.anti_porn_enabled)
    invalidate_settings(ctx.chat_id)
    await set_lock(ctx.session, ctx.chat_id, "porn", bool(settings_obj.anti_porn_enabled),
                   action=settings_obj.anti_porn_action,
                   extra={"strictness": int(settings_obj.anti_porn_strictness or 2)},
                   updated_by=ctx.user_id)
    await ctx.reply(f"{'🟢 ضدپورن فعال شد.' if settings_obj.anti_porn_enabled else '🔴 ضدپورن غیرفعال شد.'}")


# --------------------------------------------------------------------------- #
# Join requests / sticky ban / identity notices
# --------------------------------------------------------------------------- #
@command("پذیرش خودکار", "پذیرش خودکار عضویت", "تایید خودکار عضویت", "تأیید خودکار عضویت",
         role="admin", permission="manage", category="welcome",
         description="پذیرش خودکار درخواست‌های عضویت در گروه خصوصی",
         usage="پذیرش خودکار")
async def cmd_auto_approve(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.auto_approve_join_requests = not bool(settings_obj.auto_approve_join_requests)
    invalidate_settings(ctx.chat_id)
    if settings_obj.auto_approve_join_requests:
        await ctx.reply("🟢 پذیرش خودکار عضویت فعال شد.\n"
                        "از این پس درخواست‌های عضویت به‌طور خودکار تایید می‌شوند.\n"
                        "⚠️ ربات باید دسترسی «افزودن کاربران» داشته باشد.")
    else:
        await ctx.reply("🔴 پذیرش خودکار عضویت غیرفعال شد.\n"
                        "درخواست‌ها باید توسط مدیر تایید شوند.")


@command("بن ماندگار", "بن دائمی خروج", role="admin", permission="manage",
         category="moderation",
         description="خروج/بن ماندگار: کاربر اخراج‌شده دیگر نمی‌تواند بازگردد",
         usage="بن ماندگار")
async def cmd_sticky_ban(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.sticky_ban = not bool(settings_obj.sticky_ban)
    invalidate_settings(ctx.chat_id)
    if settings_obj.sticky_ban:
        await ctx.reply("🔒 بن ماندگار فعال شد.\n"
                        "در سوپرگروه، تلگرام تا زمان «رفع بن» ورود دوباره با لینک را مسدود می‌کند؛ "
                        "در گروه معمولی این تضمین وجود ندارد.\n"
                        "اگر ورود دوباره‌ای به ربات گزارش شود، ربات او را دوباره بن می‌کند.")
    else:
        await ctx.reply("🔓 بن ماندگار غیرفعال شد.\n"
                        "اخراج (کیک) به حالت عادی برگشت و کاربر می‌تواند دوباره جوین شود.")


@command("اعلام تغییر نام", "اطلاع تغییر نام", role="admin", permission="manage",
         category="welcome", description="اعلام خودکار تغییر نام کاربران در گروه",
         usage="اعلام تغییر نام")
async def cmd_announce_name(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.announce_name_change = not bool(settings_obj.announce_name_change)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢' if settings_obj.announce_name_change else '🔴'} "
                    f"اعلام تغییر نام کاربران "
                    f"{'فعال' if settings_obj.announce_name_change else 'غیرفعال'} شد.")


@command("اعلام تغییر آیدی", "اطلاع تغییر آیدی", "اعلام تغییر یوزرنیم",
         role="admin", permission="manage", category="welcome",
         description="اعلام خودکار تغییر آیدی کاربران در گروه",
         usage="اعلام تغییر آیدی")
async def cmd_announce_username(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    settings_obj = await get_settings(ctx.session, ctx.chat_id)
    settings_obj.announce_username_change = not bool(settings_obj.announce_username_change)
    invalidate_settings(ctx.chat_id)
    await ctx.reply(f"{'🟢' if settings_obj.announce_username_change else '🔴'} "
                    f"اعلام تغییر آیدی کاربران "
                    f"{'فعال' if settings_obj.announce_username_change else 'غیرفعال'} شد.")
