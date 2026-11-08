"""Filters (blocklist + custom triggers), notes, personal commands, magic triggers."""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..core.normalization import normalize_text, to_persian_digits
from ..db.models import FilterRule
from ..services import filters as filter_service
from ..services import notes as note_service
from ..services import personal as personal_service
from ..services.moderation import PUNISHMENT_OPTIONS_FA
from .common import CommandContext, require
from .registry import command

logger = logging.getLogger("armando.handlers.filters")

MATCH_MODES = {
    "کلمه": "word", "واژه": "word",
    "دقیق": "exact",
    "شامل": "contains", "بخشی": "contains",
    "الگو": "regex", "regex": "regex",
}

ACTION_MAP_FA = {
    "حذف": "delete", "حذف پیام": "delete",
    "اخطار": "warn", "سکوت": "mute", "سکوت موقت": "temp_mute",
    "اخراج": "kick", "بن": "ban", "بن موقت": "temp_ban",
    "حذف و اخطار": "delete_warn", "حذف + اخطار": "delete_warn",
    "حذف و سکوت": "delete_mute", "حذف + سکوت": "delete_mute",
    "حذف و بن": "delete_ban", "حذف + بن": "delete_ban",
    "حذف و اخراج": "delete_kick", "حذف + اخراج": "delete_kick",
    "هیچ": "none", "بدون اقدام": "none",
}


def _extract_media(message) -> tuple[str, str | None, str]:
    """Return ``(content_type, file_id, caption)`` of a message (text if none)."""
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption or ""
    if message.video:
        return "video", message.video.file_id, message.caption or ""
    if message.animation:
        return "animation", message.animation.file_id, message.caption or ""
    if message.audio:
        return "audio", message.audio.file_id, message.caption or ""
    if message.voice:
        return "voice", message.voice.file_id, message.caption or ""
    if message.video_note:
        return "video_note", message.video_note.file_id, ""
    if message.sticker:
        return "sticker", message.sticker.file_id, ""
    if message.document:
        return "document", message.document.file_id, message.caption or ""
    return "text", None, message.text or message.caption or ""


# --------------------------------------------------------------------------- #
# Blocklist / custom filters
# --------------------------------------------------------------------------- #
@command("فیلتر کردن", "فیلتر", "بلاک", "مسدود کردن کلمه", role="moderator",
         permission="filters", category="filters", description="افزودن کلمه فیلتر یا پاسخ خودکار",
         usage="فیلتر کردن تبلیغ | فیلتر سلام = خوش آمدید")
async def cmd_add_filter(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission="filters"):
        return
    raw_text = (ctx.message.text or "").strip()
    if "=" in raw_text:
        await _add_custom_filter(ctx, raw_text)
        return
    word = ctx.arg_text.strip()
    if not word:
        await ctx.reply("🚫 قالب: <code>فیلتر کردن کلمه</code> یا <code>فیلتر کلمه = پاسخ</code>")
        return
    try:
        rule = await filter_service.add_rule(ctx.session, ctx.chat_id, word, is_blocklist=True,
                                            match_mode=ctx.settings.get("filter_default_mode", "word"),
                                            action=ctx.settings.get("filter_default_action", "delete"),
                                            created_by=ctx.user_id)
    except ValueError:
        await ctx.reply("⚠️ این کلمه قبلاً فیلتر شده است.")
        return
    await ctx.reply(f"🚫 کلمه «<code>{rule.trigger}</code>» به لیست فیلتر اضافه شد.\n"
                    f"🎯 برخورد: {PUNISHMENT_OPTIONS_FA.get(rule.action, rule.action)}")


async def _add_custom_filter(ctx: CommandContext, raw_text: str) -> None:
    """Handle ``فیلتر <trigger> = <response>`` (supports replied media)."""
    from ..core.normalization import normalize_text

    normalized = raw_text
    _, _, remainder = normalized.partition("=")
    # Strip a leading "فیلتر کردن"/"فیلتر"
    head = normalized.split("=")[0]
    trigger = normalize_text(head, mode="command")
    for prefix in ("فیلتر کردن", "فیلتر"):
        if trigger.startswith(normalize_text(prefix, mode="command")):
            trigger = trigger[len(normalize_text(prefix, mode="command")):].strip()
            break
    response = remainder.strip()
    if not trigger:
        await ctx.reply("⚠️ قالب: <code>فیلتر کلمه = پاسخ</code>")
        return

    content_type, file_id, caption = ("text", None, "")
    reply = ctx.message.reply_to_message
    if reply is not None and not response:
        content_type, file_id, caption = _extract_media(reply)
        response = caption
    try:
        rule = await filter_service.add_rule(
            ctx.session, ctx.chat_id, trigger, is_blocklist=False, match_mode="contains",
            action="none", response_text=response if content_type == "text" else "",
            response_type=content_type, response_file_id=file_id,
            response_caption=caption if content_type != "text" else None,
            created_by=ctx.user_id)
    except ValueError:
        await ctx.reply("⚠️ این محرک قبلاً ثبت شده است.")
        return
    await ctx.reply(f"✨ پاسخ خودکار برای «<code>{rule.trigger}</code>» ذخیره شد.")


@command("حذف فیلتر", "پاک کردن فیلتر", "حذف بلاک", role="moderator", permission="filters",
         category="filters", description="حذف یک کلمه از فیلتر")
async def cmd_remove_filter(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission="filters"):
        return
    word = ctx.arg_text.strip()
    if not word:
        await ctx.reply("⚠️ قالب: <code>حذف فیلتر کلمه</code>")
        return
    removed = await filter_service.remove_rule(ctx.session, ctx.chat_id, word)
    await ctx.reply("🗑 فیلتر حذف شد." if removed else "ℹ️ چنین فیلتری پیدا نشد.")


@command("لیست فیلتر", "فیلترها", "لیست فیلترها", "لیست بلاک", role="moderator",
         category="filters", description="نمایش لیست کلمات فیلتر شده")
async def cmd_list_filters(ctx: CommandContext) -> None:
    rules = await filter_service.get_rules(ctx.session, ctx.chat_id)
    text = filter_service.rules_text(rules, blocklist=True, title="🚫 لیست کلمات فیلتر شده")
    custom = [r for r in rules if not r.is_blocklist]
    if custom:
        text += "\n\n" + filter_service.rules_text(rules, blocklist=False,
                                                   title="✨ پاسخ‌های خودکار")
    await ctx.reply(text)


@command("پاکسازی فیلترها", "حذف همه فیلترها", role="admin", permission="manage",
         category="filters", description="حذف همه فیلترها")
async def cmd_clear_filters(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    count = await filter_service.clear_rules(ctx.session, ctx.chat_id, blocklist=True, custom=False)
    await ctx.reply(f"🧹 {to_persian_digits(str(count))} فیلتر حذف شد.")


@command("حالت فیلتر", role="moderator", permission="filters", category="filters",
         description="تغییر حالت تطبیق یک فیلتر", usage="حالت فیلتر تبلیغ دقیق")
async def cmd_filter_mode(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission="filters"):
        return
    if len(ctx.args) < 2:
        await ctx.reply("⚙️ قالب: <code>حالت فیلتر کلمه دقیق</code>\n"
                        "حالت‌ها: کلمه، دقیق، شامل، الگو")
        return
    trigger = normalize_text(ctx.args[0], mode="command")
    mode = MATCH_MODES.get(normalize_text(ctx.args[1], mode="command"))
    if mode is None:
        await ctx.reply("❌ حالت نامعتبر. حالت‌ها: کلمه، دقیق، شامل، الگو")
        return
    result = await ctx.session.execute(
        select(FilterRule).where(FilterRule.chat_id == ctx.chat_id,
                                 FilterRule.trigger == trigger)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        await ctx.reply("❌ فیلتر پیدا نشد.")
        return
    rule.match_mode = mode
    filter_service.invalidate_filters(ctx.chat_id)
    await ctx.reply(f"✅ حالت تطبیق فیلتر «<code>{trigger}</code>» روی «{ctx.args[1]}» تنظیم شد.")


@command("اقدام فیلتر", role="moderator", permission="filters", category="filters",
         description="تعیین برخورد یک کلمه فیلتر", usage="اقدام فیلتر تبلیغ بن")
async def cmd_filter_action(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission="filters"):
        return
    if len(ctx.args) < 2:
        await ctx.reply("🎯 قالب: <code>اقدام فیلتر تبلیغ بن</code>")
        return
    trigger = normalize_text(ctx.args[0], mode="command")
    action = ACTION_MAP_FA.get(normalize_text(" ".join(ctx.args[1:]), mode="command"))
    if action is None:
        await ctx.reply("❌ برخورد نامعتبر است.")
        return
    result = await ctx.session.execute(
        select(FilterRule).where(FilterRule.chat_id == ctx.chat_id, FilterRule.trigger == trigger)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        await ctx.reply("❌ فیلتر پیدا نشد.")
        return
    rule.action = action
    filter_service.invalidate_filters(ctx.chat_id)
    await ctx.reply(f"✅ برخورد فیلتر «<code>{trigger}</code>» روی "
                    f"{PUNISHMENT_OPTIONS_FA.get(action, action)} تنظیم شد.")


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #
@command("ذخیره", "سیو", "ذخیره یادداشت", role="helper", permission="notes",
         category="notes", description="ذخیره یادداشت", usage="ذخیره قوانین = متن قوانین")
async def cmd_save_note(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    raw = (ctx.message.text or ctx.message.caption or "").strip()
    reply = ctx.message.reply_to_message
    if "=" in raw:
        head, _, body = raw.partition("=")
        name = note_service.normalize_note_name(head.replace("ذخیره", "").replace("سیو", ""))
        if not name:
            await ctx.reply("⚠️ قالب: <code>ذخیره قوانین = متن قوانین</code>")
            return
        content_type, file_id, caption = "text", None, ""
        if reply is not None and not body.strip():
            content_type, file_id, caption = _extract_media(reply)
            body = caption
        await note_service.save_note(ctx.session, ctx.chat_id, name, content_type=content_type,
                                     text=body.strip() if content_type == "text" else "",
                                     file_id=file_id, caption=caption or None,
                                     created_by=ctx.user_id)
        await ctx.reply(f"📝 یادداشت «<code>#{name}</code>» ذخیره شد.\n"
                        f"برای دریافت: <code>گرفتن {name}</code> یا <code>#{name}</code>")
        return
    # No "=" -> save the replied media under the given name
    if reply is None:
        await ctx.reply("⚠️ برای ذخیره رسانه، روی آن ریپلای کنید و بنویسید: <code>ذخیره نام</code>")
        return
    name = note_service.normalize_note_name(ctx.arg_text)
    if not name:
        await ctx.reply("⚠️ نام یادداشت را بنویسید. مثال: <code>ذخیره قوانین</code>")
        return
    content_type, file_id, caption = _extract_media(reply)
    await note_service.save_note(ctx.session, ctx.chat_id, name, content_type=content_type,
                                 text="" if content_type != "text" else (reply.text or ""),
                                 file_id=file_id, caption=caption or None,
                                 created_by=ctx.user_id)
    await ctx.reply(f"📝 یادداشت «<code>#{name}</code>» ذخیره شد.")


@command("گرفتن", "نمایش", "یادداشت", role="member", category="notes",
         description="دریافت یک یادداشت", usage="گرفتن قوانین")
async def cmd_get_note(ctx: CommandContext) -> None:
    name = note_service.normalize_note_name(ctx.arg_text)
    if not name:
        await ctx.reply("⚠️ نام یادداشت را بنویسید. مثال: <code>گرفتن قوانین</code>")
        return
    note = await note_service.get_note(ctx.session, ctx.chat_id, name)
    if note is None:
        await ctx.reply("ℹ️ چنین یادداشتی پیدا نشد.")
        return
    from ..services.render import build_context, send_content

    context = await build_context(ctx.session, ctx.bot, ctx.chat_id, ctx.user_id,
                                  chat_title=ctx.chat_title, user=ctx.message.from_user)
    await send_content(ctx.bot, ctx.chat_id, content_type=note.content_type, text=note.text,
                       file_id=note.file_id, caption=note.caption, buttons=note.buttons,
                       context=context, reply_to_message_id=ctx.message.message_id)


@command("حذف یادداشت", "پاک کردن یادداشت", role="helper", permission="notes",
         category="notes", description="حذف یک یادداشت")
async def cmd_delete_note(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    name = note_service.normalize_note_name(ctx.arg_text)
    removed = await note_service.delete_note(ctx.session, ctx.chat_id, name)
    await ctx.reply("🗑 یادداشت حذف شد." if removed else "ℹ️ یادداشتی با این نام پیدا نشد.")


@command("لیست یادداشت‌ها", "لیست یادداشتها", "یادداشت‌ها", "یادداشتها",
         role="member", category="notes", description="نمایش لیست یادداشت‌ها")
async def cmd_list_notes(ctx: CommandContext) -> None:
    notes = await note_service.list_notes(ctx.session, ctx.chat_id)
    await ctx.reply(note_service.notes_text(notes))


@command("پاکسازی یادداشت‌ها", "حذف همه یادداشت‌ها", role="admin", permission="manage",
         category="notes", description="حذف همه یادداشت‌ها")
async def cmd_clear_notes(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin", permission="manage"):
        return
    count = await note_service.clear_notes(ctx.session, ctx.chat_id)
    await ctx.reply(f"🧹 {to_persian_digits(str(count))} یادداشت حذف شد.")


# --------------------------------------------------------------------------- #
# Personal commands & magic triggers
# --------------------------------------------------------------------------- #
@command("افزودن دستور", "دستور جدید", role="helper", permission="notes",
         category="notes", description="تعریف دستور شخصی",
         usage="افزودن دستور قیمت = لیست قیمت...")
async def cmd_add_command(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    raw = (ctx.message.text or "").strip()
    if "=" not in raw:
        await ctx.reply("⚠️ قالب: <code>افزودن دستور قیمت = متن پاسخ</code>")
        return
    head, _, body = raw.partition("=")
    trigger = personal_service.normalize_trigger(head.replace("افزودن دستور", "").strip())
    if not trigger:
        await ctx.reply("⚠️ نام دستور را مشخص کنید.")
        return
    content_type, file_id, caption = "text", None, ""
    reply = ctx.message.reply_to_message
    if reply is not None and not body.strip():
        content_type, file_id, caption = _extract_media(reply)
        body = caption
    await personal_service.add_command(ctx.session, ctx.chat_id, trigger,
                                       content_type=content_type,
                                       text=body.strip() if content_type == "text" else "",
                                       file_id=file_id, caption=caption or None,
                                       created_by=ctx.user_id)
    await ctx.reply(f"⌨️ دستور «<code>{trigger}</code>» ثبت شد.")


@command("حذف دستور", role="helper", permission="notes", category="notes",
         description="حذف دستور شخصی")
async def cmd_remove_command(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    removed = await personal_service.remove_command(ctx.session, ctx.chat_id, ctx.arg_text)
    await ctx.reply("🗑 دستور حذف شد." if removed else "ℹ️ چنین دستوری پیدا نشد.")


@command("لیست دستورات", "دستورات", "لیست دستورات شخصی", role="member",
         category="notes", description="نمایش دستورات شخصی")
async def cmd_list_commands(ctx: CommandContext) -> None:
    commands = await personal_service.list_commands(ctx.session, ctx.chat_id)
    await ctx.reply(personal_service.commands_text(commands))


@command("افزودن استیکر", "استیکر", role="helper", permission="notes",
         category="notes", description="اختصاص پاسخ به استیکر/گیف (با ریپلای)")
async def cmd_add_magic(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    reply = ctx.message.reply_to_message
    if reply is None:
        await ctx.reply("⚠️ روی استیکر یا گیف ریپلای کنید و پاسخ را بنویسید.\n"
                        "مثال: <code>افزودن استیکر خوش آمدید</code>")
        return
    if reply.sticker:
        trigger_type, unique_id = "sticker", reply.sticker.file_unique_id
    elif reply.animation:
        trigger_type, unique_id = "gif", reply.animation.file_unique_id
    else:
        await ctx.reply("⚠️ پیام ریپلایی باید استیکر یا گیف باشد.")
        return
    body = ctx.arg_text.strip()
    content_type = "text"
    if not body:
        await ctx.reply("⚠️ متن پاسخ را بنویسید.")
        return
    await personal_service.add_magic(ctx.session, ctx.chat_id, trigger_type=trigger_type,
                                     file_unique_id=unique_id, content_type=content_type,
                                     text=body, created_by=ctx.user_id)
    await ctx.reply("🎴 محرک استیکر/گیف ذخیره شد.")


@command("لیست استیکرها", "محرک‌ها", "لیست محرک‌ها", role="member",
         category="notes", description="نمایش محرک‌های استیکر و گیف")
async def cmd_list_magic(ctx: CommandContext) -> None:
    items = await personal_service.list_magic(ctx.session, ctx.chat_id)
    if not items:
        await ctx.reply("🎴 هیچ محرکی ثبت نشده است.")
        return
    lines = ["🎴 <b>محرک‌های استیکر و گیف</b>", ""]
    for index, item in enumerate(items, start=1):
        label = "استیکر" if item.trigger_type == "sticker" else "گیف"
        lines.append(f"{to_persian_digits(str(index))}. {label} <code>{item.id}</code> — {item.text[:40]}")
    lines.append("")
    lines.append("برای حذف: <code>حذف محرک ۱</code>")
    await ctx.reply("\n".join(lines))


@command("حذف محرک", role="helper", permission="notes", category="notes",
         description="حذف محرک استیکر/گیف")
async def cmd_remove_magic(ctx: CommandContext) -> None:
    if not await require(ctx, role="helper", permission="notes"):
        return
    from ..core.normalization import normalize_digits

    raw = normalize_digits(ctx.arg_text, to="ascii")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        await ctx.reply("⚠️ شناسه محرک را وارد کنید: <code>حذف محرک ۱</code>")
        return
    removed = await personal_service.remove_magic(ctx.session, ctx.chat_id, int(digits))
    await ctx.reply("🗑 محرک حذف شد." if removed else "ℹ️ محرکی با این شناسه پیدا نشد.")
