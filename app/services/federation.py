"""Optional federations: groups sharing a configurable ban list."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_ban, safe_unban
from ..core.timeutils import persian_datetime
from ..db.models import (
    BanRecord,
    ChatSettings,
    Federation,
    FederationAdmin,
    FederationMember,
    ModerationAction,
)
from .chat_state import invalidate_settings

logger = logging.getLogger("armando.federation")

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")


async def get_fed(session: AsyncSession, name: str) -> Federation | None:
    result = await session.execute(select(Federation).where(Federation.name == name.lower()))
    return result.scalar_one_or_none()


async def get_fed_by_id(session: AsyncSession, fed_id: int) -> Federation | None:
    return await session.get(Federation, fed_id)


async def create_fed(session: AsyncSession, name: str, owner_id: int) -> Federation:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("⛔️ نام فدراسیون باید ۳ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد، - و _ باشد.")
    if await get_fed(session, name) is not None:
        raise ValueError("⛔️ این نام قبلاً استفاده شده است.")
    fed = Federation(name=name, owner_id=owner_id, created_at=datetime.utcnow())
    session.add(fed)
    await session.flush()
    session.add(FederationAdmin(federation_id=fed.id, user_id=owner_id, added_by=owner_id))
    await session.flush()
    return fed


async def delete_fed(session: AsyncSession, fed: Federation) -> None:
    await session.execute(delete(FederationAdmin).where(FederationAdmin.federation_id == fed.id))
    await session.execute(delete(FederationMember).where(FederationMember.federation_id == fed.id))
    await session.execute(delete(BanRecord).where(BanRecord.federation_id == fed.id))
    await session.delete(fed)
    await session.flush()


async def join_fed(session: AsyncSession, fed: Federation, chat_id: int,
                   joined_by: int | None = None) -> bool:
    result = await session.execute(
        select(FederationMember).where(FederationMember.federation_id == fed.id,
                                       FederationMember.chat_id == chat_id)
    )
    if result.scalar_one_or_none() is not None:
        return False
    session.add(FederationMember(federation_id=fed.id, chat_id=chat_id, joined_by=joined_by))
    settings_obj = await session.get(ChatSettings, chat_id)
    if settings_obj is not None:
        settings_obj.federation_id = fed.id
        invalidate_settings(chat_id)
    await session.flush()
    return True


async def leave_fed(session: AsyncSession, chat_id: int) -> bool:
    result = await session.execute(
        select(FederationMember).where(FederationMember.chat_id == chat_id)
    )
    member = result.scalar_one_or_none()
    if member is None:
        return False
    await session.delete(member)
    settings_obj = await session.get(ChatSettings, chat_id)
    if settings_obj is not None:
        settings_obj.federation_id = None
        settings_obj.fed_enforce = False
        invalidate_settings(chat_id)
    await session.flush()
    return True


async def is_fed_admin(session: AsyncSession, fed: Federation, user_id: int) -> bool:
    if fed.owner_id == user_id:
        return True
    result = await session.execute(
        select(FederationAdmin).where(FederationAdmin.federation_id == fed.id,
                                      FederationAdmin.user_id == user_id)
    )
    return result.scalar_one_or_none() is not None


async def add_fed_admin(session: AsyncSession, fed: Federation, user_id: int,
                        added_by: int | None = None) -> bool:
    if await is_fed_admin(session, fed, user_id):
        return False
    session.add(FederationAdmin(federation_id=fed.id, user_id=user_id, added_by=added_by))
    await session.flush()
    return True


async def remove_fed_admin(session: AsyncSession, fed: Federation, user_id: int) -> bool:
    if fed.owner_id == user_id:
        return False
    result = await session.execute(
        delete(FederationAdmin).where(FederationAdmin.federation_id == fed.id,
                                      FederationAdmin.user_id == user_id)
    )
    await session.flush()
    return bool(result.rowcount)


async def fed_admins(session: AsyncSession, fed: Federation) -> list[int]:
    result = await session.execute(
        select(FederationAdmin.user_id).where(FederationAdmin.federation_id == fed.id)
    )
    return [row[0] for row in result.all()]


async def fed_chats(session: AsyncSession, fed: Federation) -> list[int]:
    result = await session.execute(
        select(FederationMember.chat_id).where(FederationMember.federation_id == fed.id)
    )
    return [row[0] for row in result.all()]


async def fban(session: AsyncSession, bot: Bot, fed: Federation, *, user_id: int,
               reason: str, banned_by: int, duration: int | None = None,
               enforce: bool = False) -> int:
    """Federation ban. Returns how many chats were enforced immediately."""
    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None
    result = await session.execute(
        select(BanRecord).where(BanRecord.federation_id == fed.id, BanRecord.user_id == user_id,
                                BanRecord.scope == "fed", BanRecord.active.is_(True))
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = BanRecord(federation_id=fed.id, user_id=user_id, banned_by=banned_by,
                           reason=(reason or "")[:500], scope="fed", active=True, until=until,
                           created_at=datetime.utcnow())
        session.add(record)
    else:
        record.reason = (reason or "")[:500]
        record.until = until
        record.active = True
        record.banned_by = banned_by
    await session.flush()

    applied = 0
    if enforce:
        chat_ids = await fed_chats(session, fed)
        # One bulk read instead of a SELECT per member chat.
        enforcing: set[int] = set()
        if chat_ids:
            rows = (await session.execute(
                select(ChatSettings.chat_id).where(
                    ChatSettings.chat_id.in_(list(chat_ids)),
                    ChatSettings.fed_enforce.is_(True)))).scalars().all()
            enforcing = {int(row) for row in rows}
        for chat_id in chat_ids:
            if chat_id not in enforcing:
                continue
            ok = await safe_ban(bot, chat_id, user_id, until_date=until, context="fban_enforce")
            if ok:
                applied += 1
                federation_reason = f"[فدراسیون {fed.name}] {reason or ''}"[:500]
                session.add(ModerationAction(
                    chat_id=chat_id, target_id=user_id, actor_id=banned_by, action="ban",
                    reason=federation_reason, duration=duration,
                    expires_at=until, active=True, source="federation",
                    created_at=datetime.utcnow()))
                try:
                    from .ban_reports import report_successful_ban

                    await report_successful_ban(
                        bot, session, chat_id=chat_id, target_id=user_id,
                        actor_id=banned_by, reason=federation_reason,
                        duration=duration, source="federation",
                        action="temp_ban" if duration else "ban")
                except Exception as exc:  # noqa: BLE001 - continue enforcing other chats
                    logger.warning("federation-ban report failed chat=%s (%s)",
                                   chat_id, exc.__class__.__name__)
    await session.flush()
    return applied


async def funban(session: AsyncSession, bot: Bot, fed: Federation, *, user_id: int,
                 unbanned_by: int) -> bool:
    result = await session.execute(
        select(BanRecord).where(BanRecord.federation_id == fed.id, BanRecord.user_id == user_id,
                                BanRecord.scope == "fed", BanRecord.active.is_(True))
    )
    record = result.scalar_one_or_none()
    if record is None:
        return False
    record.active = False
    await session.flush()
    for chat_id in await fed_chats(session, fed):
        await safe_unban(bot, chat_id, user_id, only_if_banned=True, context="funban")
        session.add(ModerationAction(chat_id=chat_id, target_id=user_id, actor_id=unbanned_by,
                                     action="unban", reason=f"[فدراسیون {fed.name}]",
                                     active=False, source="federation",
                                     created_at=datetime.utcnow()))
    await session.flush()
    return True


async def is_fbanned(session: AsyncSession, fed_id: int, user_id: int) -> BanRecord | None:
    result = await session.execute(
        select(BanRecord).where(BanRecord.federation_id == fed_id, BanRecord.user_id == user_id,
                                BanRecord.scope == "fed", BanRecord.active.is_(True))
    )
    return result.scalar_one_or_none()


async def fban_list(session: AsyncSession, fed: Federation, limit: int = 50) -> list[BanRecord]:
    result = await session.execute(
        select(BanRecord).where(BanRecord.federation_id == fed.id, BanRecord.scope == "fed",
                                BanRecord.active.is_(True))
        .order_by(BanRecord.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


def fed_info_text(fed: Federation, chats: list[int], admins: list[int], bans: int) -> str:
    from ..core.normalization import to_persian_digits

    return "\n".join([
        "🌐 <b>اطلاعات فدراسیون</b>",
        "",
        f"📛 نام: <code>{fed.name}</code>",
        f"👑 مالک: <code>{fed.owner_id}</code>",
        f"💬 تعداد گروه‌ها: {to_persian_digits(str(len(chats)))}",
        f"👮 تعداد مدیران: {to_persian_digits(str(len(admins)))}",
        f"🚫 تعداد بن‌ها: {to_persian_digits(str(bans))}",
        f"⚙️ حالت اجرا: {'اجرای خودکار' if fed.ban_mode == 'enforce' else 'فقط اشتراک‌گذاری'}",
        f"🕒 تاریخ ایجاد: {persian_datetime(fed.created_at)}",
    ])


def fban_list_text(records: list[BanRecord]) -> str:
    from ..core.normalization import to_persian_digits

    if not records:
        return "🚫 لیست بن فدراسیون خالی است."
    lines = ["🚫 <b>بن‌های فدراسیون</b>", ""]
    for index, record in enumerate(records, start=1):
        until = persian_datetime(record.until) if record.until else "همیشگی"
        lines.append(f"{to_persian_digits(str(index))}. <code>{record.user_id}</code>\n"
                     f"   📌 {record.reason or '—'}\n"
                     f"   ⏱ تا: {until}")
    return "\n".join(lines)
