"""Bulk message cleanup with correct handling of Telegram deletion limits."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger("armando.purge")

BATCH = 100
SLEEP_BETWEEN_BATCHES = 0.35


@dataclass
class PurgeResult:
    deleted: int = 0
    failed: int = 0
    too_old: int = 0
    limit_hit: bool = False

    @property
    def attempted(self) -> int:
        return self.deleted + self.failed + self.too_old


async def _delete_one(bot: Bot, chat_id: int, message_id: int, result: PurgeResult) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        result.deleted += 1
        return True
    except TelegramBadRequest as exc:
        message = (exc.message or "").lower()
        if "too old" in message or "can't be deleted" in message:
            result.too_old += 1
        else:
            result.failed += 1
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("delete failed: %s", exc)
        result.failed += 1
        return False


async def purge_range(bot: Bot, chat_id: int, start_id: int, end_id: int) -> PurgeResult:
    """Delete messages from ``start_id`` up to and including ``end_id``."""
    result = PurgeResult()
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    total = end_id - start_id + 1
    if total > 2000:
        end_id = start_id + 2000
        result.limit_hit = True

    message_ids = list(range(start_id, end_id + 1))
    for index in range(0, len(message_ids), BATCH):
        batch = message_ids[index:index + BATCH]
        for message_id in batch:
            await _delete_one(bot, chat_id, message_id, result)
        if index + BATCH < len(message_ids):
            await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
    return result


async def purge_count(bot: Bot, chat_id: int, count: int, until_message_id: int) -> PurgeResult:
    """Delete ``count`` messages ending at ``until_message_id`` (inclusive)."""
    count = max(1, min(int(count), 1000))
    start = max(1, until_message_id - count + 1)
    return await purge_range(bot, chat_id, start, until_message_id)


def purge_summary(result: PurgeResult, *, silent: bool = False) -> str:
    from ..core.normalization import to_persian_digits

    if silent:
        return ""
    lines = ["🧹 <b>عملیات پاکسازی انجام شد</b>", "",
             f"🗑 حذف‌شده: {to_persian_digits(str(result.deleted))}"]
    if result.too_old:
        lines.append(f"⏳ قدیمی‌تر از ۴۸ ساعت (غیرقابل حذف): {to_persian_digits(str(result.too_old))}")
    if result.failed:
        lines.append(f"⚠️ ناموفق: {to_persian_digits(str(result.failed))}")
    if result.limit_hit:
        lines.append("ℹ️ تعداد درخواستی زیاد بود؛ فقط ۲۰۰۰ پیام اخیر بررسی شد.")
    if result.too_old or result.failed:
        lines.append("")
        lines.append("ℹ️ تلگرام اجازه حذف پیام‌های قدیمی‌تر از ۴۸ ساعت را در برخی گروه‌ها نمی‌دهد.")
    return "\n".join(lines)
