"""Chat-scoped owner delegation; never grants Telegram permissions."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from typing import Iterable

from aiogram import Bot
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.normalization import normalize_digits
from ..db.models import BotRootGrant, Chat, RootGrantDraft, User
from ..keyboards.factory import cb, danger, markup, primary, row, success

USER_ID_RE = re.compile(r"^\d{5,15}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")
CHAT_TYPES = ("group", "supergroup", "channel")

# Everything the bot currently exposes as a bot-owner capability is listed
# here. All new grants start with no boxes checked; older grants keep their
# pre-existing broadcast-only capability through the database migration.
ROOT_PERMISSION_LABELS: dict[str, str] = {
    "broadcast": "ارسال همگانی در چت واگذارشده",
    "global_ban": "بن سراسری، رفع بن و فهرست بن‌ها",
    "global_mute": "سکوت سراسری، رفع سکوت و فهرست سکوت‌ها",
    "bot_stats": "مشاهدهٔ آمار کلی ربات",
    "root_manage": "اعطا، لغو و فهرست دسترسی روت در همین چت",
    "approval_manage": "تأیید/رد همین گروه و مشاهدهٔ وضعیت تأیید",
    "leave_chat": "خروج ربات از همین گروه",
}
ROOT_PERMISSION_KEYS = tuple(ROOT_PERMISSION_LABELS)
LEGACY_ROOT_PERMISSIONS = ("broadcast",)
CHANNEL_ROOT_PERMISSIONS = ("broadcast", "global_ban", "global_mute", "bot_stats")
ROOT_PAGE_SIZE = 8


class RootAccessError(ValueError):
    """Safe Persian validation error shown to the bot owner."""


def is_bot_owner(user_id: int) -> bool:
    return bool(settings.owner_id and int(user_id) == int(settings.owner_id))


def normalize_permissions(permissions: Iterable[str] | None) -> list[str]:
    """Return known permissions in stable UI order; unknown values fail closed."""
    supplied = set(permissions or ())
    return [key for key in ROOT_PERMISSION_KEYS if key in supplied]


def permissions_available_for_chat(chat_type: str) -> list[str]:
    """Only offer actions that can operate in the selected chat type."""
    if chat_type == "channel":
        return list(CHANNEL_ROOT_PERMISSIONS)
    if chat_type in {"group", "supergroup"}:
        return list(ROOT_PERMISSION_KEYS)
    return []


def permission_mask(permissions: Iterable[str] | None) -> int:
    selected = set(normalize_permissions(permissions))
    return sum(1 << index for index, key in enumerate(ROOT_PERMISSION_KEYS)
               if key in selected)


def permissions_from_mask(mask: int, *,
                          allowed_permissions: Iterable[str] | None = None) -> list[str]:
    try:
        bits = max(0, int(mask))
    except (TypeError, ValueError):
        return []
    allowed = set(normalize_permissions(allowed_permissions)) if allowed_permissions is not None else set(ROOT_PERMISSION_KEYS)
    return [key for index, key in enumerate(ROOT_PERMISSION_KEYS)
            if bits & (1 << index) and key in allowed]


def selected_permissions_text(permissions: Iterable[str] | None) -> str:
    labels = [ROOT_PERMISSION_LABELS[key] for key in normalize_permissions(permissions)]
    return "، ".join(labels) if labels else "بدون دسترسی"


def validate_target_ref(target_ref: str) -> str:
    raw = (target_ref or "").strip()
    normalized = normalize_digits(raw, to="ascii")
    if USER_ID_RE.fullmatch(normalized):
        return normalized
    if USERNAME_RE.fullmatch(raw):
        return "@" + raw.lstrip("@").lower()
    raise RootAccessError("❌ هدف باید شناسهٔ عددی یا نام‌کاربری معتبر تلگرام باشد.")


async def get_root_grant(session: AsyncSession, *, chat_id: int,
                         user_id: int) -> BotRootGrant | None:
    result = await session.execute(
        select(BotRootGrant).where(BotRootGrant.chat_id == int(chat_id),
                                   BotRootGrant.user_id == int(user_id)).limit(1))
    return result.scalar_one_or_none()


async def permissions_for(session: AsyncSession, *, chat_id: int,
                          user_id: int) -> list[str]:
    grant = await get_root_grant(session, chat_id=chat_id, user_id=user_id)
    return normalize_permissions(grant.permissions if grant is not None else None)


async def has_root_permission(session: AsyncSession, *, chat_id: int,
                              user_id: int, permission: str) -> bool:
    if permission not in ROOT_PERMISSION_LABELS:
        return False
    return permission in await permissions_for(session, chat_id=chat_id, user_id=user_id)


async def grant_root(session: AsyncSession, *, chat_id: int, user_id: int,
                     granted_by: int,
                     permissions: Iterable[str] | None = None) -> bool:
    """Create/update a capability grant that is scoped to exactly one chat.

    ``None`` preserves the old API's broadcast-only behavior. The owner menu
    always supplies an explicit selection, and an empty selection is rejected.
    Returns ``True`` only when a new grant row was created.
    """
    selected = normalize_permissions(
        LEGACY_ROOT_PERMISSIONS if permissions is None else permissions)
    if not selected:
        raise RootAccessError("❌ دست‌کم یک اختیار را انتخاب کنید.")
    existing = await get_root_grant(session, chat_id=chat_id, user_id=user_id)
    if existing is not None:
        existing.permissions = selected
        existing.granted_by = int(granted_by)
        existing.created_at = datetime.utcnow()
        await session.flush()
        return False
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, first_name=""))
        await session.flush()
    session.add(BotRootGrant(chat_id=chat_id, user_id=user_id,
                             granted_by=granted_by, permissions=selected,
                             created_at=datetime.utcnow()))
    await session.flush()
    return True


async def revoke_root(session: AsyncSession, *, chat_id: int, user_id: int) -> bool:
    result = await session.execute(
        delete(BotRootGrant).where(BotRootGrant.chat_id == chat_id,
                                   BotRootGrant.user_id == user_id))
    return bool(result.rowcount)


async def revoke_root_everywhere(session: AsyncSession, *, user_id: int) -> int:
    result = await session.execute(
        delete(BotRootGrant).where(BotRootGrant.user_id == user_id))
    return int(result.rowcount or 0)


async def root_chat_ids(session: AsyncSession, user_id: int, *,
                        active_only: bool = True,
                        permission: str | None = "broadcast") -> list[int]:
    query = (select(BotRootGrant.chat_id, BotRootGrant.permissions)
             .join(Chat, Chat.id == BotRootGrant.chat_id)
             .where(BotRootGrant.user_id == user_id,
                    Chat.type.in_(CHAT_TYPES)))
    if active_only:
        query = query.where(Chat.is_active.is_(True))
    result = await session.execute(query.order_by(BotRootGrant.chat_id))
    rows = result.all()
    return [int(chat_id) for chat_id, permissions in rows
            if permission is None or permission in normalize_permissions(permissions)]


async def known_active_chats(session: AsyncSession) -> list[Chat]:
    """Chats the bot has recorded as active (Bot API has no all-chats query)."""
    result = await session.execute(
        select(Chat).where(Chat.is_active.is_(True), Chat.type.in_(CHAT_TYPES))
        .order_by(Chat.title, Chat.id))
    return list(result.scalars().all())


async def chats_for_root(session: AsyncSession, user_id: int, *,
                         permission: str | None = "broadcast") -> list[Chat]:
    ids = await root_chat_ids(session, user_id, permission=permission)
    if not ids:
        return []
    result = await session.execute(
        select(Chat).where(Chat.id.in_(ids), Chat.is_active.is_(True),
                           Chat.type.in_(CHAT_TYPES)).order_by(Chat.title, Chat.id))
    return list(result.scalars().all())


async def can_use_root_permission(bot: Bot, session: AsyncSession, *,
                                  chat_id: int, user_id: int,
                                  permission: str) -> bool:
    """Owner bypass, otherwise require a scoped grant and current admin status."""
    if is_bot_owner(user_id):
        return True
    if not await has_root_permission(session, chat_id=chat_id, user_id=user_id,
                                     permission=permission):
        return False
    return await is_current_admin_or_creator(bot, chat_id, user_id)


async def create_root_draft(session: AsyncSession, *, owner_id: int,
                            target_ref: str) -> RootGrantDraft:
    target_ref = validate_target_ref(target_ref)
    await session.execute(delete(RootGrantDraft).where(RootGrantDraft.owner_id == owner_id))
    draft = RootGrantDraft(owner_id=owner_id, target_ref=target_ref[:64],
                           created_at=datetime.utcnow(),
                           expires_at=datetime.utcnow() + timedelta(minutes=20))
    session.add(draft)
    await session.flush()
    return draft


async def get_root_draft(session: AsyncSession, draft_id: int,
                         owner_id: int) -> RootGrantDraft | None:
    draft = await session.get(RootGrantDraft, draft_id)
    if (draft is None or int(draft.owner_id) != int(owner_id)
            or draft.expires_at < datetime.utcnow()):
        return None
    return draft


async def list_root_grants(session: AsyncSession, *,
                           chat_id: int | None = None):
    query = (select(BotRootGrant, Chat, User)
             .join(Chat, Chat.id == BotRootGrant.chat_id)
             .join(User, User.id == BotRootGrant.user_id))
    if chat_id is not None:
        query = query.where(BotRootGrant.chat_id == int(chat_id))
    result = await session.execute(
        query.order_by(Chat.title, User.first_name, BotRootGrant.user_id))
    return list(result.all())


def root_picker_text(draft: RootGrantDraft, chats: list[Chat], page: int) -> str:
    pages = max(1, (len(chats) + ROOT_PAGE_SIZE - 1) // ROOT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    user_ref = html.escape(draft.target_ref)
    return ("🔐 <b>اعطای دسترسی روت محدود به یک چت</b>\n\n"
            f"هدف: <code>{user_ref}</code>\n"
            "چت مقصد را انتخاب کنید. سپس اختیارات را جداگانه تیک می‌زنید؛ "
            "ادمین هدف باید مدیر یا مالک فعلی همان چت باشد.\n"
            "چت‌های فعالِ ثبت‌شده نمایش داده می‌شوند؛ رابط برنامه‌نویسی ربات تلگرام فهرست کامل چت‌ها را ارائه نمی‌کند.\n\n"
            f"صفحهٔ {page + 1} از {pages}")


def root_picker_keyboard(draft: RootGrantDraft, chats: list[Chat], page: int):
    pages = max(1, (len(chats) + ROOT_PAGE_SIZE - 1) // ROOT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * ROOT_PAGE_SIZE
    rows = []
    for chat in chats[start:start + ROOT_PAGE_SIZE]:
        title = " ".join((chat.title or "").split()) or (f"@{chat.username}" if chat.username else str(chat.id))
        icon = "📢" if chat.type == "channel" else "👥"
        rows.append([primary(f"{icon} {title[:36]}", cb("rt", "grant", draft.id, chat.id))])
    if len(chats) > ROOT_PAGE_SIZE:
        rows.append(row(
            primary("⬅️ قبلی", cb("rt", "page", draft.id, max(0, page - 1)))
            if page > 0 else None,
            primary(f"{page + 1}/{pages}", cb("rt", "noop")),
            primary("بعدی ➡️", cb("rt", "page", draft.id, min(pages - 1, page + 1)))
            if page + 1 < pages else None,
        ))
    rows.append([danger("❌ لغو", cb("rt", "cancel", draft.id))])
    return markup(rows)


def root_permission_text(*, target_name: str, user_id: int, chat: Chat | None,
                         selected_permissions: Iterable[str] | None,
                         allowed_permissions: Iterable[str] | None = None) -> str:
    allowed = normalize_permissions(allowed_permissions)
    selected = normalize_permissions(selected_permissions)
    chat_name = (chat.title or (f"@{chat.username}" if chat.username else str(chat.id))) if chat else "چت انتخاب‌شده"
    selected_text = selected_permissions_text(selected)
    return ("🔐 <b>انتخاب اختیارات واگذارشده</b>\n\n"
            f"👤 ادمین: {html.escape(target_name or str(user_id))} (<code>{user_id}</code>)\n"
            f"💬 چت: {html.escape(chat_name)}\n\n"
            "برای هر اختیار موردنظر دکمه را بزنید؛ موردی به‌صورت خودکار فعال نیست.\n"
            f"✅ انتخاب فعلی: {html.escape(selected_text)}\n\n"
            "🔒 این واگذاری فقط در همین چت معتبر است و فقط به مدیر/سازندهٔ فعلی آن داده می‌شود. "
            "اگر بن یا سکوت سراسری را انتخاب کنید، اثر عملیات سراسری خواهد بود؛ "
            "برای سکوت، ربات باید مدیرِ دارای حق محدودسازی باشد و مالک بات در سوپرگروه حضور داشته باشد.\n"
            "⚠️ مدیریت روت، تأیید گروه و خروج ربات اختیارات جداگانه‌اند؛ فقط در صورت تیک‌زدن فعال می‌شوند.")


def root_permission_keyboard(draft: RootGrantDraft, *, chat_id: int, user_id: int,
                             selected_permissions: Iterable[str] | None = None,
                             allowed_permissions: Iterable[str] | None = None):
    allowed = normalize_permissions(allowed_permissions)
    if allowed_permissions is None:
        allowed = list(ROOT_PERMISSION_KEYS)
    selected = [key for key in normalize_permissions(selected_permissions) if key in allowed]
    mask = permission_mask(selected)
    rows = []
    for index, key in enumerate(ROOT_PERMISSION_KEYS):
        if key not in allowed:
            continue
        enabled = key in selected
        next_mask = mask ^ (1 << index)
        marker = "✅" if enabled else "▫️"
        rows.append([primary(
            f"{marker} {ROOT_PERMISSION_LABELS[key]}",
            cb("rt", "toggle", draft.id, chat_id, user_id, next_mask))])
    if selected:
        rows.append([success(
            "💾 ثبت اختیارات انتخاب‌شده",
            cb("rt", "save", draft.id, chat_id, user_id, mask))])
    else:
        rows.append([primary("تیک‌زدن دست‌کم یک اختیار", cb("rt", "noop"))])
    rows.append([danger("❌ لغو", cb("rt", "cancel", draft.id))])
    return markup(rows)


async def resolve_admin_target(bot: Bot, session: AsyncSession, chat_id: int,
                               target_ref: str) -> tuple[int, str, str | None]:
    """Resolve an id/username and require current Telegram admin/creator status.

    Usernames are resolved only from this chat's administrator list or the
    bot's own observed user records; Bot API itself accepts a numeric user id.
    """
    raw = (target_ref or "").strip()
    normalized = normalize_digits(raw, to="ascii")
    user_id: int | None = None
    if USER_ID_RE.fullmatch(normalized):
        user_id = int(normalized)
    else:
        match = USERNAME_RE.fullmatch(raw)
        if not match:
            raise RootAccessError(
                "❌ هدف را با شناسهٔ عددی یا نام‌کاربری معتبر وارد کنید.")
        username = match.group(0).lstrip("@").lower()

        try:
            admins = await bot.get_chat_administrators(chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001 - fail closed when current admin list is unavailable
            admins = None
            logger_message = exc.__class__.__name__
        else:
            logger_message = ""
        for admin in admins or []:
            admin_user = getattr(admin, "user", None)
            if admin_user is not None and (admin_user.username or "").lower() == username:
                user_id = int(admin_user.id)
                break
        if user_id is None:
            result = await session.execute(
                select(User).where(User.username.is_not(None)).limit(2000))
            for user in result.scalars().all():
                if (user.username or "").lower() == username:
                    user_id = int(user.id)
                    break
        if user_id is None and logger_message:
            raise RootAccessError(
                "⚠️ فهرست مدیران این چت قابل بررسی نیست؛ شناسهٔ عددی را بدهید یا دسترسی ربات را بررسی کنید.")
        if user_id is None:
            raise RootAccessError(
                f"❌ نام‌کاربری «@{username}» در کاربران شناخته‌شده یا مدیران این چت پیدا نشد؛ "
                "شناسهٔ عددی را امتحان کنید.")

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - fail closed when status is unknown
        raise RootAccessError(
            "⚠️ وضعیت مدیریتی این کاربر در چت قابل بررسی نیست؛ "
            "ربات باید به چت دسترسی داشته باشد.") from exc
    status = str(getattr(member, "status", ""))
    if status not in {"administrator", "creator"}:
        raise RootAccessError(
            "⛔️ دسترسی روت فقط به مدیر فعلی یا مالک همان گروه/کانال داده می‌شود.")
    user = getattr(member, "user", None)
    name = (getattr(user, "full_name", "") or "").strip() or str(user_id)
    return user_id, name, getattr(user, "username", None)


async def is_current_admin_or_creator(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Recheck a delegated user's current status before scoped owner actions."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:  # noqa: BLE001 - fail closed
        return False
    return str(getattr(member, "status", "")) in {"administrator", "creator"}
