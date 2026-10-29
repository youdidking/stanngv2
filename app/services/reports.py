"""User reporting workflow: report -> admin notification with quick actions."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..config import settings
from ..core.timeutils import persian_datetime
from ..db.models import Report
from ..keyboards.factory import cb, danger, primary, success, btn

logger = logging.getLogger("armando.reports")


async def create_report(session: AsyncSession, *, chat_id: int, reporter_id: int,
                        target_id: int | None, message_id: int | None,
                        reason: str = "") -> Report:
    report = Report(
        chat_id=chat_id,
        reporter_id=reporter_id,
        target_id=target_id,
        message_id=message_id,
        reason=(reason or "بدون توضیح")[:500],
        status="open",
        created_at=datetime.utcnow(),
    )
    session.add(report)
    await session.flush()
    return report


async def resolve_report(session: AsyncSession, report_id: int, status: str,
                         handled_by: int | None = None) -> Report | None:
    report = await session.get(Report, report_id)
    if report is None:
        return None
    report.status = status
    report.handled_by = handled_by
    report.resolved_at = datetime.utcnow()
    await session.flush()
    return report


def report_keyboard(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [danger("🔨 بن", cb("rep", report_id, "ban")),
         primary("🔇 سکوت", cb("rep", report_id, "mute")),
         primary("⚠️ اخطار", cb("rep", report_id, "warn"))],
        [primary("🗑 حذف پیام", cb("rep", report_id, "delete")),
         success("✅ بی‌اقدام", cb("rep", report_id, "ignore"))],
        [btn("❌ بستن گزارش", cb("rep", report_id, "close"))],
    ])


def report_text(*, chat_title: str, reporter_name: str, reporter_id: int,
                target_name: str, target_id: int | None, reason: str,
                message_preview: str = "") -> str:
    lines = [
        "📢 <b>گزارش جدید</b>",
        "",
        f"💬 گروه: {chat_title}",
        f"🙋 گزارش‌دهنده: {reporter_name} (<code>{reporter_id}</code>)",
        f"🎯 کاربر گزارش‌شده: {target_name}" + (f" (<code>{target_id}</code>)" if target_id else ""),
        f"📌 دلیل: {reason}",
        f"🕒 زمان: {persian_datetime()}",
    ]
    if message_preview:
        lines.append("")
        lines.append(f"📝 متن پیام:\n{message_preview[:400]}")
    return "\n".join(lines)


async def notify_admins(bot: Bot, session: AsyncSession, *, chat_id: int, report: Report,
                        chat_title: str, reporter_name: str, target_name: str,
                        message_preview: str = "") -> None:
    text = report_text(chat_title=chat_title, reporter_name=reporter_name,
                       reporter_id=report.reporter_id, target_name=target_name,
                       target_id=report.target_id, reason=report.reason,
                       message_preview=message_preview)
    keyboard = report_keyboard(report.id)

    # 1) the dedicated log channel / report channel of the group
    from .chat_state import get_settings

    settings_obj = await get_settings(session, chat_id)
    targets: list[int] = []
    if settings_obj.log_chat_id:
        targets.append(int(settings_obj.log_chat_id))
    if settings.log_chat_id and int(settings.log_chat_id) not in targets:
        targets.append(int(settings.log_chat_id))

    # 2) group administrators (private notification, best effort)
    if settings_obj.report_notify_admins:
        try:
            admins = await bot.get_chat_administrators(chat_id=chat_id)
            for admin in admins:
                if admin.user.is_bot:
                    continue
                targets.append(admin.user.id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not list admins for report: %s", exc)

    for target in dict.fromkeys(targets):
        await safe_call(lambda t=target: bot.send_message(chat_id=t, text=text,
                                                          parse_mode=ParseMode.HTML,
                                                          reply_markup=keyboard),
                        context="report_notify")


async def open_reports(session: AsyncSession, chat_id: int, limit: int = 10) -> list[Report]:
    result = await session.execute(
        select(Report).where(Report.chat_id == chat_id, Report.status == "open")
        .order_by(Report.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
