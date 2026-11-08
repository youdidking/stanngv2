"""Preview-and-confirm multi-ban for a replied-to list of IDs/usernames."""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_answer, safe_edit
from ..core.message_lifecycle import GROUP_BOT_MESSAGE_TTL
from ..core.normalization import normalize_digits, to_persian_digits
from ..keyboards.factory import cb, danger, markup, parse_cb, primary
from ..services import moderation as moderation_service
from ..services.permissions import build_actor, check_moderation_target, get_bot_actor
from ..services.targeting import resolve_target_reference
from .common import CommandContext, ensure_bot_admin, require
from .registry import command

router = Router(name="multi_ban")
logger = logging.getLogger("armando.handlers.multi_ban")

MAX_MULTIBAN_TARGETS = 25
_DRAFT_TTL_SECONDS = GROUP_BOT_MESSAGE_TTL
_MAX_DRAFTS = 100
_ID_RE = re.compile(r"^\d{5,15}$")
_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
_IGNORED_LABELS = {"شناسه", "شناسه‌ها", "کاربر", "کاربران", "فهرست", "users", "ids"}


@dataclass(frozen=True)
class BatchTarget:
    user_id: int
    name: str
    ref: str


@dataclass
class MultiBanDraft:
    token: str
    chat_id: int
    requester_id: int
    chat_title: str
    targets: list[BatchTarget]
    reason: str
    created_at: float
    processing: bool = False


_DRAFTS: dict[str, MultiBanDraft] = {}


def parse_target_references(raw: str) -> tuple[list[str], list[str]]:
    """Parse a list containing numeric IDs and @usernames; ignore list markers."""
    normalized = normalize_digits(raw or "", to="ascii")
    references: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw_token in re.split(r"[\s,،;؛|]+", normalized):
        token = raw_token.strip().lstrip("•-*_").strip("`'\"()[]{}<>.,،;؛:!؟…")
        if not token or token.lower() in _IGNORED_LABELS:
            continue
        if re.fullmatch(r"\d{1,3}\.", raw_token):
            continue
        if _ID_RE.fullmatch(token):
            value = token
        elif _USERNAME_RE.fullmatch(token):
            value = "@" + token[1:].lower()
        else:
            invalid.append(token[:64])
            continue
        key = value.lower()
        if key not in seen:
            seen.add(key)
            references.append(value)
    return references, invalid


def _prune_drafts(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    for token, draft in list(_DRAFTS.items()):
        if now - draft.created_at > _DRAFT_TTL_SECONDS and not draft.processing:
            _DRAFTS.pop(token, None)
    if len(_DRAFTS) > _MAX_DRAFTS:
        oldest = sorted(_DRAFTS.values(), key=lambda draft: draft.created_at)
        for draft in oldest[:len(_DRAFTS) - _MAX_DRAFTS]:
            if not draft.processing:
                _DRAFTS.pop(draft.token, None)


def _preview_keyboard(token: str):
    return markup([[
        primary("✅ تأیید و شروع بن‌ها", cb("mb", "confirm", token)),
        danger("✖️ لغو", cb("mb", "cancel", token)),
    ]])


def _format_preview(chat_title: str, accepted: list[BatchTarget],
                    rejected: list[tuple[str, str]], invalid: list[str],
                    reason: str) -> str:
    lines = [
        "⚠️ <b>پیش‌نمایش مولتی‌بن</b>",
        f"گروه: <b>{html.escape(chat_title or 'بدون نام')}</b>",
        f"قابل اقدام: <b>{to_persian_digits(str(len(accepted)))}</b> از حداکثر {MAX_MULTIBAN_TARGETS}",
        "",
    ]
    if accepted:
        lines.append("<b>هدف‌های آمادهٔ بن:</b>")
        for target in accepted:
            lines.append(f"• {html.escape(target.name)} — <code>{target.user_id}</code>")
    if rejected:
        lines.extend(["", "<b>ردشده یا شناسایی‌نشده:</b>"])
        for ref, why in rejected[:10]:
            lines.append(f"• <code>{html.escape(ref)}</code> — {html.escape(why)}")
        if len(rejected) > 10:
            lines.append(f"• و {to_persian_digits(str(len(rejected) - 10))} مورد دیگر…")
    if invalid:
        lines.extend(["", f"موارد نامعتبر نادیده‌گرفته‌شده: {to_persian_digits(str(len(invalid)))}"])
    if reason:
        lines.extend(["", f"دلیل بن: {html.escape(reason[:300])}"])
    lines.extend(["", "برای اجرای عملیات، «تأیید» را بزنید؛ بدون تأیید هیچ‌کس بن نمی‌شود."])
    return "\n".join(lines)


@command("مولتی بن", "مولتی‌بن", "بن چندگانه", role="moderator", permission="ban",
         category="moderation", description="بن چند کاربر با فهرست شناسه‌ها یا نام‌کاربری‌ها",
         usage="روی فهرست ID/@username ریپلای کنید و بنویسید: مولتی بن [دلیل]",
         group_only=True)
async def cmd_multi_ban(ctx: CommandContext) -> None:
    if not await require(ctx, role="moderator", permission="ban"):
        return
    if not await ensure_bot_admin(ctx, permission="ban"):
        return
    if not ctx.is_group:
        await ctx.reply("⛔️ مولتی‌بن فقط در گروه قابل اجراست.")
        return
    source = ctx.message.reply_to_message
    if source is None or getattr(source, "sender_chat", None) is not None:
        await ctx.reply(
            "⚠️ روی پیامی که فهرست شناسه‌ها یا نام‌کاربری‌ها را دارد ریپلای کنید.\n"
            "هر هدف را جداگانه بنویسید؛ نمونه:\n<code>123456789\n@username</code>")
        return
    raw = (getattr(source, "text", None) or getattr(source, "caption", None) or "").strip()
    references, invalid = parse_target_references(raw)
    if not references:
        await ctx.reply("❌ شناسهٔ عددی یا نام‌کاربری معتبر پیدا نشد.")
        return
    if len(references) > MAX_MULTIBAN_TARGETS:
        await ctx.reply(
            f"⚠️ هر نوبت حداکثر {to_persian_digits(str(MAX_MULTIBAN_TARGETS))} هدف دارد؛ "
            "فهرست را به چند بخش تقسیم کنید.")
        return

    accepted: list[BatchTarget] = []
    rejected: list[tuple[str, str]] = []
    seen_ids: set[int] = set()
    for reference in references:
        target = await resolve_target_reference(ctx.bot, ctx.session, ctx.chat_id, reference)
        if not target.ok or target.user_id is None:
            rejected.append((reference, target.error or "هدف شناسایی نشد"))
            continue
        if target.user_id in seen_ids:
            continue
        seen_ids.add(target.user_id)
        denied = await check_moderation_target(
            ctx.bot, ctx.session, ctx.chat_id, target.user_id, ctx.actor)
        if denied:
            rejected.append((reference, denied))
            continue
        accepted.append(BatchTarget(
            user_id=int(target.user_id),
            name=target.full_name or reference,
            ref=reference,
        ))

    if not accepted:
        await ctx.reply(_format_preview(ctx.chat_title, accepted, rejected, invalid, ctx.arg_text))
        return

    _prune_drafts()
    token = secrets.token_hex(6)
    draft = MultiBanDraft(
        token=token,
        chat_id=ctx.chat_id,
        requester_id=ctx.user_id,
        chat_title=ctx.chat_title,
        targets=accepted,
        reason=ctx.arg_text[:300],
        created_at=time.monotonic(),
    )
    _DRAFTS[token] = draft
    preview = _format_preview(ctx.chat_title, accepted, rejected, invalid, draft.reason)
    await ctx.reply(preview, reply_markup=_preview_keyboard(token))
    await ctx.delete_invocation()


@router.callback_query(F.data.startswith("mb:"))
async def on_multi_ban_callback(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = parse_cb(callback.data or "")
    action = parts[1] if len(parts) > 1 else ""
    token = parts[2] if len(parts) > 2 else ""
    draft = _DRAFTS.get(token)
    if draft is None:
        await safe_answer(callback, "⌛️ پیش‌نمایش منقضی شده است؛ دستور را دوباره اجرا کنید.",
                          show_alert=True)
        return
    message = callback.message
    if (message is None or message.chat.id != draft.chat_id
            or message.chat.type not in {"group", "supergroup"}):
        await safe_answer(callback, "⛔️ این دکمه برای این گروه معتبر نیست.", show_alert=True)
        return
    if callback.from_user.id != draft.requester_id:
        await safe_answer(callback, "⛔️ فقط درخواست‌کننده می‌تواند این عملیات را تأیید یا لغو کند.",
                          show_alert=True)
        return
    if time.monotonic() - draft.created_at > _DRAFT_TTL_SECONDS:
        _DRAFTS.pop(token, None)
        await safe_answer(callback, "⌛️ پیش‌نمایش منقضی شده است.", show_alert=True)
        return
    if action == "cancel":
        _DRAFTS.pop(token, None)
        await safe_answer(callback, "عملیات لغو شد.")
        await safe_edit(message, "✖️ <b>مولتی‌بن لغو شد؛ هیچ‌کس بن نشد.</b>",
                        parse_mode="HTML", reply_markup=None)
        return
    if action != "confirm":
        await safe_answer(callback)
        return
    if draft.processing:
        await safe_answer(callback, "⏳ این عملیات در حال اجراست.", show_alert=True)
        return
    draft.processing = True

    chat_id = draft.chat_id
    actor = await build_actor(bot, chat_id, callback.from_user.id, session=session,
                              chat_type=message.chat.type)
    if not actor.has_role("moderator") or not actor.can_ban():
        _DRAFTS.pop(token, None)
        await safe_answer(callback, "⛔️ دسترسی بن شما تغییر کرده است.", show_alert=True)
        await safe_edit(message, "⛔️ دسترسی بن کافی نیست؛ هیچ بن گروهی اجرا نشد.",
                        parse_mode="HTML", reply_markup=None)
        return
    bot_actor = await get_bot_actor(bot, chat_id)
    if not bot_actor.can_ban():
        _DRAFTS.pop(token, None)
        await safe_answer(callback, "⛔️ ربات دسترسی بن ندارد.", show_alert=True)
        await safe_edit(message, "⛔️ ربات دسترسی بن ندارد؛ هیچ بن گروهی اجرا نشد.",
                        parse_mode="HTML", reply_markup=None)
        return

    successes: list[BatchTarget] = []
    failures: list[tuple[BatchTarget, str]] = []
    try:
        for target in draft.targets:
            try:
                result = await moderation_service.ban_user(
                    bot, session, chat_id=chat_id, actor=actor,
                    target_id=target.user_id, target_name=target.name,
                    reason=draft.reason or "مولتی‌بن",
                    chat_title=draft.chat_title, source="multi_ban")
                if result.startswith("🔨"):
                    await session.commit()
                    successes.append(target)
                else:
                    failures.append((target, result))
            except Exception as exc:  # noqa: BLE001 - keep earlier successful bans committed
                await session.rollback()
                logger.exception("multi-ban failed chat=%s requester=%s target=%s error=%s",
                                 chat_id, draft.requester_id, target.user_id,
                                 exc.__class__.__name__)
                failures.append((target, "خطای داخلی هنگام اجرای این بن؛ ادامه متوقف شد."))
                break
    finally:
        _DRAFTS.pop(token, None)

    lines = [
        "🛡 <b>گزارش مولتی‌بن</b>",
        f"گروه: <b>{html.escape(draft.chat_title or 'بدون نام')}</b> — <code>{chat_id}</code>",
        f"✅ بن موفق: {to_persian_digits(str(len(successes)))}",
        f"⚠️ ردشده یا ناموفق: {to_persian_digits(str(len(failures)))}",
    ]
    for target, why in failures[:8]:
        if target.user_id:
            lines.append(f"• {html.escape(target.name)} (<code>{target.user_id}</code>): {html.escape(why[:180])}")
        else:
            lines.append(f"• {html.escape(why)}")
    if len(failures) > 8:
        lines.append(f"• و {to_persian_digits(str(len(failures) - 8))} مورد دیگر…")
    await safe_answer(callback, "✅ عملیات تمام شد.")
    await safe_edit(message, "\n".join(lines), parse_mode="HTML", reply_markup=None)
