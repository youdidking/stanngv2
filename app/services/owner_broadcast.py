"""Persistent, owner-reviewed broadcast selection and control helpers."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.normalization import to_persian_digits
from ..db.models import BotSettings, BroadcastDraft, Chat
from ..keyboards.factory import cb, danger, markup, primary, row, success
from . import owner_access

PAGE_SIZE = 8
DRAFT_TTL = timedelta(hours=1)
CHAT_TYPES = ("group", "supergroup", "channel")


def is_bot_owner(user_id: int) -> bool:
    return bool(settings.owner_id and int(user_id) == int(settings.owner_id))


async def get_bot_settings(session: AsyncSession) -> BotSettings:
    state = await session.get(BotSettings, 1)
    if state is None:
        state = BotSettings(id=1, broadcast_enabled=True)
        session.add(state)
        await session.flush()
    return state


async def authorized_chats(session: AsyncSession, user_id: int) -> list[Chat]:
    if is_bot_owner(user_id):
        return await owner_access.known_active_chats(session)
    return await owner_access.chats_for_root(session, user_id)


async def effective_chats(session: AsyncSession, user_id: int, *,
                          message_chat_id: int, message_chat_type: str) -> list[Chat]:
    """Private flows may choose destinations; group flows are chat-local."""
    chats = await authorized_chats(session, user_id)
    if message_chat_type in CHAT_TYPES:
        return [chat for chat in chats if int(chat.id) == int(message_chat_id)]
    return chats


async def create_draft(session: AsyncSession, *, owner_id: int,
                       source_chat_id: int | None,
                       source_message_ids: list[int] | None,
                       text_body: str | None,
                       selected_chat_ids: list[int] | None = None) -> BroadcastDraft:
    await session.execute(
        delete(BroadcastDraft).where(BroadcastDraft.owner_id == owner_id,
                                     BroadcastDraft.stage.in_(["select", "review"])))
    draft = BroadcastDraft(
        owner_id=owner_id,
        source_chat_id=source_chat_id,
        source_message_ids=list(source_message_ids or []),
        text_body=text_body,
        selected_chat_ids=[int(item) for item in (selected_chat_ids or [])],
        stage="select",
        page=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + DRAFT_TTL,
    )
    session.add(draft)
    await session.flush()
    return draft


async def get_draft(session: AsyncSession, draft_id: int,
                    owner_id: int) -> BroadcastDraft | None:
    draft = await session.get(BroadcastDraft, draft_id)
    if draft is None or int(draft.owner_id) != int(owner_id):
        return None
    if draft.stage not in {"select", "review"} or draft.expires_at < datetime.utcnow():
        return None
    return draft


async def cancel_draft(session: AsyncSession, draft: BroadcastDraft) -> None:
    draft.stage = "cancelled"
    draft.updated_at = datetime.utcnow()
    await session.flush()


def _chat_label(chat: Chat, *, escape_text: bool = True) -> str:
    title = " ".join((chat.title or "").split())
    if not title and chat.username:
        title = f"@{chat.username}"
    if not title:
        title = str(chat.id)
    title = title[:36]
    if escape_text:
        title = html.escape(title)
    kind = "📢" if chat.type == "channel" else "👥"
    return f"{kind} {title}"


def _private_report_delay_row(delay: int, draft_id: int) -> list:
    delay = max(0, min(600, int(delay)))
    label = "خاموش" if delay == 0 else f"{to_persian_digits(str(delay))} ثانیه"
    return row(
        primary("➖ ۱ث", cb("bc", "delay", draft_id, -1)),
        primary(f"⏱ حذف گزارش خصوصی: {label}", cb("bc", "noop")),
        success("➕ ۱ث", cb("bc", "delay", draft_id, 1)),
    )


def _selection_text(draft: BroadcastDraft, chats: list[Chat], enabled: bool,
                    *, delegated: bool) -> str:
    selected_ids = {int(item) for item in (draft.selected_chat_ids or [])}
    selected = [chat for chat in chats if int(chat.id) in selected_ids]
    lines = [
        "📢 <b>انتخاب مقصدهای ارسال همگانی</b>",
        "",
        f"وضعیت ارسال همگانی: {'🟢 روشن' if enabled else '🔴 خاموش'}",
        f"چت‌های انتخاب‌شده: {len(selected)} از {len(chats)}",
        "ℹ️ رابط برنامه‌نویسی ربات تلگرام (Bot API) فهرست کامل چت‌ها را نمی‌دهد؛ این فهرست فقط چت‌های فعالِ ثبت‌شده را نشان می‌دهد.",
    ]
    if delegated:
        lines.append("🔐 فقط چت‌هایی در دسترس‌اند که مالک ربات برای شما تعیین کرده و هنوز در آن‌ها مدیر هستید.")
    if draft.source_chat_id is not None and draft.source_message_ids:
        lines.append("\n📝 پیام پاسخ‌داده‌شده برای مقصدهای انتخابی کپی می‌شود.")
    elif draft.text_body:
        preview = html.escape(draft.text_body[:220])
        if len(draft.text_body) > 220:
            preview += "…"
        lines.extend(["\n📝 <b>پیش‌نمایش متن:</b>", preview])
    lines.append("\nبا دکمه‌ها مقصدها را انتخاب کنید؛ ارسال تا تأیید نهایی انجام نمی‌شود.")
    return "\n".join(lines)


def selection_keyboard(draft: BroadcastDraft, chats: list[Chat], enabled: bool,
                       *, delegated: bool, report_delete_after: int = 12) -> object:
    if not enabled:
        rows = []
        if not delegated:
            rows.append([success("🟢 روشن‌کردن ارسال همگانی", cb("bc", "switch", draft.id))])
            rows.append(_private_report_delay_row(report_delete_after, draft.id))
        rows.append([danger("لغو", cb("bc", "cancel", draft.id))])
        return markup(rows)
    pages = max(1, (len(chats) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(draft.page or 0), pages - 1))
    start = page * PAGE_SIZE
    selected = {int(item) for item in (draft.selected_chat_ids or [])}
    rows = []
    for chat in chats[start:start + PAGE_SIZE]:
        check = "✅" if int(chat.id) in selected else "▫️"
        rows.append([primary(
            f"{check} {_chat_label(chat, escape_text=False)}",
            cb("bc", "pick", draft.id, chat.id, page),
        )])
    if len(chats) > PAGE_SIZE:
        rows.append(row(
            primary("⬅️ قبلی", cb("bc", "page", draft.id, max(0, page - 1)))
            if page > 0 else None,
            primary(f"صفحهٔ {page + 1}/{pages}", cb("bc", "noop")),
            primary("بعدی ➡️", cb("bc", "page", draft.id, min(pages - 1, page + 1)))
            if page + 1 < pages else None,
        ))
    if chats:
        rows.append(row(
            primary("✅ انتخاب همه", cb("bc", "all", draft.id)),
            primary("🧹 پاک‌کردن انتخاب", cb("bc", "clear", draft.id)),
        ))
    rows.append(row(
        success("ادامه و مرور انتخاب‌ها", cb("bc", "review", draft.id)),
        danger("لغو", cb("bc", "cancel", draft.id)),
    ))
    if not delegated:
        switch_button = danger if enabled else success
        rows.append([switch_button(
            "🔴 خاموش‌کردن ارسال همگانی" if enabled else "🟢 روشن‌کردن ارسال همگانی",
            cb("bc", "switch", draft.id),
        )])
        rows.append(_private_report_delay_row(report_delete_after, draft.id))
    return markup(rows)


def review_text(draft: BroadcastDraft, chats: list[Chat], *, delegated: bool) -> str:
    selected_ids = {int(item) for item in (draft.selected_chat_ids or [])}
    selected = [chat for chat in chats if int(chat.id) in selected_ids]
    lines = ["🧾 <b>مرور پیش از ارسال همگانی</b>", ""]
    if delegated:
        lines.append("🔐 ارسال فقط به چت‌های تحت دسترسی روت شما انجام می‌شود.")
    lines.append(f"📬 تعداد مقصدها: {len(selected)}")
    lines.append("")
    for chat in selected[:40]:
        lines.append(f"• {_chat_label(chat)}")
    if len(selected) > 40:
        lines.append(f"… و {len(selected) - 40} مقصد دیگر")
    if draft.source_chat_id is not None and draft.source_message_ids:
        lines.extend(["", "📝 پیام پاسخ‌داده‌شده کپی می‌شود."])
    elif draft.text_body:
        preview = html.escape(draft.text_body[:220])
        if len(draft.text_body) > 220:
            preview += "…"
        lines.extend(["", "📝 <b>متن:</b>", preview])
    lines.extend(["", "⚠️ فقط پس از زدن دکمهٔ تأیید، ارسال آغاز می‌شود."])
    return "\n".join(lines)


def review_keyboard(draft: BroadcastDraft, enabled: bool, *, delegated: bool,
                    report_delete_after: int = 12) -> object:
    rows = []
    if enabled:
        rows.append(row(
            success("✅ تأیید و ارسال", cb("bc", "send", draft.id)),
            primary("⬅️ بازگشت به انتخاب", cb("bc", "edit", draft.id, draft.page)),
        ))
    else:
        rows.append([primary("⬅️ بازگشت به انتخاب", cb("bc", "edit", draft.id, draft.page))])
    rows.append([danger("لغو", cb("bc", "cancel", draft.id))])
    if not delegated:
        if enabled:
            rows.append([danger("🔴 خاموش‌کردن ارسال همگانی", cb("bc", "switch", draft.id))])
        else:
            rows.append([success("🟢 روشن‌کردن ارسال همگانی", cb("bc", "switch", draft.id))])
        rows.append(_private_report_delay_row(report_delete_after, draft.id))
    return markup(rows)


def switch_keyboard(enabled: bool, report_delete_after: int = 12) -> object:
    label = "🔴 خاموش‌کردن ارسال همگانی" if enabled else "🟢 روشن‌کردن ارسال همگانی"
    style = danger if enabled else success
    return markup([
        [style(label, cb("bc", "switch", 0))],
        _private_report_delay_row(report_delete_after, 0),
    ])
