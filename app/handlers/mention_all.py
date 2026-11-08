"""«تگ همه» / «منشن همه» - mention known members with safe batches."""

from __future__ import annotations

import asyncio
import logging

from aiogram.types import ReplyParameters

from ..core.errors import safe_send
from ..services import mention_all as mention_service
from .common import CommandContext, require
from .registry import command

logger = logging.getLogger("armando.handlers.mention_all")
_ACTIVE_CHATS: set[int] = set()


@command("تگ همه", "منشن همه", "صدا زدن همه", "فراخوانی همه", "تگ اعضا", "منشن اعضا",
         "@all", role="admin", category="members", group_only=True,
         description="منشن اعضای شناخته‌شده و قابل‌تگ گروه", usage="تگ همه")
async def cmd_mention_all(ctx: CommandContext) -> None:
    if not await require(ctx, role="admin"):
        return

    caller = ctx.message.from_user
    caller_pair = None
    if caller is not None and not caller.is_bot:
        caller_pair = (caller.id, mention_service.display_name(
            caller.first_name, caller.last_name, caller.username))

    members = await mention_service.collect(ctx.session, ctx.chat_id, bot=ctx.bot,
                                            caller=caller_pair)
    if not members:
        await ctx.reply(
            "📣 عضوی برای تگ‌کردن پیدا نشد؛ ممکن است هنوز عضوی ثبت نشده باشد یا "
            "همهٔ اعضای شناخته‌شده از تگ همه مستثنا شده باشند.\n"
            "ربات فقط اعضایی را می‌شناسد که بعد از حضورش در گروه پیام فرستاده، "
            "ری‌اکت زده یا وارد شده باشند.")
        return

    batches = mention_service.build_batches(members)
    if not batches:
        return

    # Database work is complete; commit before any rate-limit sleeps so a large
    # mention run does not hold its per-update DB transaction/connection open.
    await ctx.session.commit()

    if ctx.chat_id in _ACTIVE_CHATS:
        await ctx.reply("⏳ ارسال تگ همه در این گروه هنوز ادامه دارد؛ درخواست تکراری اجرا نشد.")
        return
    _ACTIVE_CHATS.add(ctx.chat_id)

    try:
        reply_kwargs = {}
        target = ctx.message.reply_to_message
        if target is not None:
            reply_kwargs["reply_parameters"] = ReplyParameters(
                message_id=target.message_id, allow_sending_without_reply=True)
        thread_id = getattr(ctx.message, "message_thread_id", None)
        if thread_id is not None:
            reply_kwargs["message_thread_id"] = thread_id

        sent_messages = 0
        sent_mentions = 0
        for index, (text, entities) in enumerate(batches):
            # ``parse_mode=None`` is essential: the bot sets HTML as its global
            # default and Telegram then re-parses the text, throwing the mention
            # entities away. Preserve the reply target and topic for every batch.
            message = await safe_send(
                ctx.bot, ctx.chat_id, text, entities=entities,
                parse_mode=None, disable_web_page_preview=True, **reply_kwargs)
            if message is not None:
                sent_messages += 1
                sent_mentions += len(entities)
            if index + 1 < len(batches):
                await asyncio.sleep(mention_service.SEND_DELAY)

        expected_mentions = len(mention_service.mentioned_ids(batches))
        logger.info(
            "mention-all: sent %s/%s messages and %s/%s mentions (chat=%s)",
            sent_messages, len(batches), sent_mentions, expected_mentions, ctx.chat_id)
    finally:
        _ACTIVE_CHATS.discard(ctx.chat_id)
