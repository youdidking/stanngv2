"""Group statistics and activity analytics.

Only data the bot has legitimately observed is reported: messages, joins,
leaves and moderation actions recorded *after* the bot joined the chat.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_relative
from ..db.models import (
    Chat,
    ChatMemberState,
    GroupStatSnapshot,
    ModerationAction,
    User,
    UserActivity,
    Warning,
)

logger = logging.getLogger("armando.stats")


def _today() -> date:
    return datetime.utcnow().date()


async def record_message(session: AsyncSession, chat_id: int, user_id: int, *,
                         media: bool = False, command: bool = False) -> None:
    """Increment the daily activity row for a user (upsert, no ORM round-trip)."""
    day = _today()
    result = await session.execute(
        select(UserActivity).where(UserActivity.chat_id == chat_id,
                                   UserActivity.user_id == user_id,
                                   UserActivity.day == day)
    )
    row = result.scalar_one_or_none()
    if row is None:
        session.add(UserActivity(chat_id=chat_id, user_id=user_id, day=day,
                                 message_count=1,
                                 media_count=1 if media else 0,
                                 command_count=1 if command else 0))
    else:
        row.message_count = int(row.message_count or 0) + 1
        if media:
            row.media_count = int(row.media_count or 0) + 1
        if command:
            row.command_count = int(row.command_count or 0) + 1


async def record_event(session: AsyncSession, chat_id: int, field: str, amount: int = 1) -> None:
    """Increment a counter on today's chat snapshot (``joins``/``leaves``/...)."""
    day = _today()
    result = await session.execute(
        select(GroupStatSnapshot).where(GroupStatSnapshot.chat_id == chat_id,
                                        GroupStatSnapshot.day == day)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = GroupStatSnapshot(chat_id=chat_id, day=day, created_at=datetime.utcnow())
        session.add(row)
        await session.flush()
    current = int(getattr(row, field, 0) or 0)
    setattr(row, field, current + amount)


async def update_member_count(session: AsyncSession, chat_id: int, member_count: int) -> None:
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        chat.member_count = member_count
        chat.last_seen_at = datetime.utcnow()
    await record_event(session, chat_id, "member_count", 0)
    result = await session.execute(
        select(GroupStatSnapshot).where(GroupStatSnapshot.chat_id == chat_id,
                                        GroupStatSnapshot.day == _today())
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.member_count = member_count


async def snapshot(session: AsyncSession, chat_id: int, bot: Bot | None = None) -> None:
    """Take (or refresh) today's snapshot for a chat."""
    day = _today()
    result = await session.execute(
        select(GroupStatSnapshot).where(GroupStatSnapshot.chat_id == chat_id,
                                        GroupStatSnapshot.day == day)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = GroupStatSnapshot(chat_id=chat_id, day=day, created_at=datetime.utcnow())
        session.add(row)
        await session.flush()

    message_result = await session.execute(
        select(func.coalesce(func.sum(UserActivity.message_count), 0)).where(
            UserActivity.chat_id == chat_id, UserActivity.day == day)
    )
    row.message_count = int(message_result.scalar() or 0)
    active_result = await session.execute(
        select(func.count(UserActivity.id)).where(
            UserActivity.chat_id == chat_id, UserActivity.day == day,
            UserActivity.message_count > 0)
    )
    row.active_users = int(active_result.scalar() or 0)
    if bot is not None:
        chat = await session.get(Chat, chat_id)
        if chat is not None:
            try:
                count = await safe_call(lambda: bot.get_chat_member_count(chat_id=chat_id),
                                        default=None, log=False)
                if count:
                    row.member_count = int(count)
                    chat.member_count = int(count)
            except Exception:  # noqa: BLE001
                pass
    await session.flush()


async def top_users(session: AsyncSession, chat_id: int, days: int = 7,
                    limit: int = 10) -> list[tuple[int, int]]:
    since = _today() - timedelta(days=max(1, days))
    result = await session.execute(
        select(UserActivity.user_id, func.sum(UserActivity.message_count).label("total"))
        .where(UserActivity.chat_id == chat_id, UserActivity.day >= since)
        .group_by(UserActivity.user_id)
        .order_by(func.sum(UserActivity.message_count).desc())
        .limit(limit)
    )
    return [(int(row[0]), int(row[1] or 0)) for row in result.all()]


async def total_messages_since(session: AsyncSession, chat_id: int, days: int) -> int:
    since = _today() - timedelta(days=max(0, days))
    result = await session.execute(
        select(func.coalesce(func.sum(UserActivity.message_count), 0)).where(
            UserActivity.chat_id == chat_id, UserActivity.day >= since)
    )
    return int(result.scalar() or 0)


async def active_users_since(session: AsyncSession, chat_id: int, days: int) -> int:
    since = _today() - timedelta(days=max(0, days))
    result = await session.execute(
        select(func.count(func.distinct(UserActivity.user_id))).where(
            UserActivity.chat_id == chat_id, UserActivity.day >= since,
            UserActivity.message_count > 0)
    )
    return int(result.scalar() or 0)


async def inactive_users(session: AsyncSession, chat_id: int, days: int = 30,
                         limit: int = 50) -> list[tuple[int, datetime | None, int]]:
    """Users known to the bot with no activity in the last ``days`` days."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    result = await session.execute(
        select(ChatMemberState).where(
            ChatMemberState.chat_id == chat_id,
            (ChatMemberState.last_message_at.is_(None)) | (ChatMemberState.last_message_at < cutoff)
        ).order_by(ChatMemberState.last_message_at.asc().nullsfirst()).limit(limit)
    )
    rows = []
    for state in result.scalars().all():
        rows.append((state.user_id, state.last_message_at, int(state.message_count or 0)))
    return rows


async def moderation_counts(session: AsyncSession, chat_id: int, days: int = 30) -> dict[str, int]:
    since = datetime.utcnow() - timedelta(days=max(0, days))
    result = await session.execute(
        select(ModerationAction.action, func.count(ModerationAction.id))
        .where(ModerationAction.chat_id == chat_id, ModerationAction.created_at >= since)
        .group_by(ModerationAction.action)
    )
    counts = {str(row[0]): int(row[1]) for row in result.all()}
    warn_result = await session.execute(
        select(func.count(Warning.id)).where(Warning.chat_id == chat_id,
                                             Warning.created_at >= since)
    )
    counts["warns"] = int(warn_result.scalar() or 0)
    return counts


async def group_report(session: AsyncSession, chat_id: int) -> dict:
    chat = await session.get(Chat, chat_id)
    messages_today = await total_messages_since(session, chat_id, 1)
    messages_week = await total_messages_since(session, chat_id, 7)
    messages_month = await total_messages_since(session, chat_id, 30)
    active_today = await active_users_since(session, chat_id, 1)
    active_week = await active_users_since(session, chat_id, 7)
    mod = await moderation_counts(session, chat_id, 30)

    joins = 0
    leaves = 0
    result = await session.execute(
        select(func.coalesce(func.sum(GroupStatSnapshot.joins), 0),
               func.coalesce(func.sum(GroupStatSnapshot.leaves), 0))
        .where(GroupStatSnapshot.chat_id == chat_id,
               GroupStatSnapshot.day >= _today() - timedelta(days=30))
    )
    row = result.first()
    if row:
        joins, leaves = int(row[0] or 0), int(row[1] or 0)
    return {
        "title": chat.title if chat else "",
        "member_count": int(chat.member_count or 0) if chat else 0,
        "messages_today": messages_today,
        "messages_week": messages_week,
        "messages_month": messages_month,
        "active_today": active_today,
        "active_week": active_week,
        "joins": joins,
        "leaves": leaves,
        "warns": mod.get("warns", 0),
        "bans": mod.get("ban", 0) + mod.get("temp_ban", 0),
        "mutes": mod.get("mute", 0) + mod.get("temp_mute", 0),
        "kicks": mod.get("kick", 0),
        "since": chat.first_seen_at if chat else None,
    }


def format_group_report(report: dict) -> str:
    lines = [
        "📊 <b>آمار گروه</b>",
        f"💬 گروه: {report.get('title') or '—'}",
        "",
        f"👥 تعداد اعضا: {to_persian_digits(str(report.get('member_count', 0)))}",
        f"📨 پیام امروز: {to_persian_digits(str(report.get('messages_today', 0)))}",
        f"📨 پیام هفته: {to_persian_digits(str(report.get('messages_week', 0)))}",
        f"📨 پیام ماه: {to_persian_digits(str(report.get('messages_month', 0)))}",
        f"🔥 فعال امروز: {to_persian_digits(str(report.get('active_today', 0)))}",
        f"🔥 فعال هفته: {to_persian_digits(str(report.get('active_week', 0)))}",
        f"📥 ورود (۳۰ روز): {to_persian_digits(str(report.get('joins', 0)))}",
        f"📤 خروج (۳۰ روز): {to_persian_digits(str(report.get('leaves', 0)))}",
        f"⚠️ اخطارها: {to_persian_digits(str(report.get('warns', 0)))}",
        f"🔨 بن‌ها: {to_persian_digits(str(report.get('bans', 0)))}",
        f"🔇 سکوت‌ها: {to_persian_digits(str(report.get('mutes', 0)))}",
        f"👢 اخراج‌ها: {to_persian_digits(str(report.get('kicks', 0)))}",
    ]
    since = report.get("since")
    if since:
        lines.append("")
        lines.append(f"🕒 شروع ثبت آمار: {persian_relative(since)}")
        lines.append("ℹ️ آمار فقط پیام‌هایی را شامل می‌شود که ربات آن‌ها را دیده است.")
    return "\n".join(lines)


async def top_users_text(session: AsyncSession, chat_id: int, days: int = 7,
                         limit: int = 10) -> str:
    rows = await top_users(session, chat_id, days=days, limit=limit)
    if not rows:
        return (f"🏆 فعال‌ترین کاربران ({to_persian_digits(str(days))} روز اخیر)\n\n"
                "داده‌ای ثبت نشده است.")
    lines = [f"🏆 فعال‌ترین کاربران ({to_persian_digits(str(days))} روز اخیر)", ""]
    medals = ["🥇", "🥈", "🥉"]
    from .chat_state import display_names

    names = await display_names(session, [row[0] for row in rows])
    for index, (user_id, count) in enumerate(rows, start=1):
        name = names.get(int(user_id)) or str(user_id)
        medal = medals[index - 1] if index <= 3 else f"{to_persian_digits(str(index))}."
        lines.append(f"{medal} {name} — {to_persian_digits(str(count))} پیام")
    return "\n".join(lines)


async def inactive_users_text(session: AsyncSession, chat_id: int, days: int = 30,
                              limit: int = 30) -> str:
    rows = await inactive_users(session, chat_id, days=days, limit=limit)
    if not rows:
        return (f"💤 غیرفعال‌ها ({to_persian_digits(str(days))} روز اخیر)\n\n"
                "کاربر غیرفعالی در داده‌های ثبت‌شده وجود ندارد.")
    lines = [f"💤 غیرفعال‌ها ({to_persian_digits(str(days))} روز اخیر)", ""]
    from .chat_state import display_names

    names = await display_names(session, [row[0] for row in rows])
    for user_id, last_seen, count in rows:
        name = names.get(int(user_id)) or str(user_id)
        last = persian_relative(last_seen) if last_seen else "بدون فعالیت ثبت‌شده"
        lines.append(f"• {name} — آخرین فعالیت: {last} (تعداد پیام: {to_persian_digits(str(count))})")
    lines.append("")
    lines.append("ℹ️ فقط کاربرانی نمایش داده می‌شوند که ربات پیامی از آن‌ها دریافت کرده باشد.")
    return "\n".join(lines)


async def growth_chart(session: AsyncSession, chat_id: int, days: int = 30) -> bytes | None:
    """Render a member-growth chart as PNG bytes (matplotlib is optional)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 - matplotlib is an optional dependency
        logger.info("matplotlib not available; skipping chart")
        return None

    since = _today() - timedelta(days=max(2, days))
    result = await session.execute(
        select(GroupStatSnapshot).where(GroupStatSnapshot.chat_id == chat_id,
                                        GroupStatSnapshot.day >= since)
        .order_by(GroupStatSnapshot.day)
    )
    rows = list(result.scalars().all())
    if len(rows) < 2:
        return None
    xs = [row.day for row in rows]
    ys = [int(row.member_count or 0) for row in rows]
    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    ax.plot(xs, ys, marker="o", color="#2e86de", linewidth=2)
    ax.fill_between(xs, ys, color="#2e86de", alpha=0.15)
    ax.set_title("Member growth")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer.read()


async def prune_activity(session: AsyncSession, days: int = 180) -> int:
    cutoff = _today() - timedelta(days=days)
    result = await session.execute(delete(UserActivity).where(UserActivity.day < cutoff))
    await session.flush()
    return int(result.rowcount or 0)
