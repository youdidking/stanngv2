"""New member verification (button / math / emoji CAPTCHA).

CAPTCHA state is always keyed by ``chat_id + user_id`` and stored in the
database, so it survives restarts and never collides with other users' FSM
state.  Callback data only carries an opaque token.
"""

from __future__ import annotations

import logging
import random
import secrets
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import safe_call
from ..core.normalization import to_persian_digits
from ..db.models import CaptchaSession
from ..keyboards.factory import InlineKeyboardMarkup, btn
from .chat_state import get_member_state, invalidate_member
from .moderation import MUTE_PERMISSIONS, UNMUTE_PERMISSIONS

logger = logging.getLogger("armando.captcha")

EMOJI_SET = ["🚀", "🌙", "🔥", "🐱", "🍀", "🎈", "⭐️", "🌈", "🐳", "🌻", "🍉", "⚽️"]
MODE_LABELS_FA = {"button": "دکمه‌ای", "math": "ریاضی", "emoji": "ایموجی"}


def new_token() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:14]


def _math_challenge() -> tuple[str, list[str]]:
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    op = random.choice(["+", "-", "×"])
    if op == "+":
        answer = a + b
    elif op == "-":
        a, b = max(a, b), min(a, b)
        answer = a - b
    else:
        answer = a * b
    question = f"حاصل عبارت زیر چند است؟\n\n{to_persian_digits(str(a))} {op} {to_persian_digits(str(b))} = ❓"
    options = _options(str(answer), numeric=True)
    return question, options


def _options(answer: str, numeric: bool = False, emoji: bool = False) -> list[str]:
    pool: list[str] = [answer]
    while len(pool) < 4:
        if numeric:
            candidate = str(int(answer) + random.choice([-3, -2, -1, 1, 2, 3, 4, 5]))
            if int(candidate) <= 0:
                continue
        elif emoji:
            candidate = random.choice(EMOJI_SET)
        else:
            candidate = random.choice(["من ربات هستم", "من یک بازدیدکننده‌ام", "تأیید نمی‌کنم", "خروج"])
        if candidate not in pool:
            pool.append(candidate)
    random.shuffle(pool)
    return pool[:4]


async def create_session(session: AsyncSession, chat_id: int, user_id: int, *, mode: str,
                         timeout: int, max_attempts: int) -> CaptchaSession:
    if mode == "math":
        question, options = _math_challenge()
        answer = options[0]  # placeholder, resolved below
        # recompute the real answer deterministically
        answer = _math_answer(question)
        text = question
    elif mode == "emoji":
        answer = random.choice(EMOJI_SET)
        options = _options(answer, emoji=True)
        text = f"روی ایموجی درست بزن تا عضویتت تأیید شود:\n\n👉 {answer}"
    else:
        answer = "تأیید می‌کنم انسان هستم"
        options = _options(answer)
        text = "برای ادامه در گروه، روی دکمه درست بزن."

    record = CaptchaSession(
        token=new_token(),
        chat_id=chat_id,
        user_id=user_id,
        mode=mode,
        answer=str(answer),
        options=options,
        max_attempts=int(max_attempts or 3),
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(seconds=int(timeout or 120)),
    )
    session.add(record)
    await session.flush()

    # Store the rendered question so the handler can reuse it verbatim.
    record.payload_text = text  # type: ignore[attr-defined]
    return record


def _math_answer(question: str) -> str:
    import re

    from ..core.normalization import normalize_digits

    raw = normalize_digits(question, to="ascii")
    match = re.search(r"(\d+)\s*([+\-×])\s*(\d+)", raw)
    if not match:
        return "0"
    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return str(a * b)


def captcha_text(session_record: CaptchaSession, user_name: str, chat_title: str,
                 attempts_left: int, timeout: int) -> str:
    mode_label = MODE_LABELS_FA.get(session_record.mode, session_record.mode)
    return (
        f"🔐 <b>تأیید عضویت</b>\n\n"
        f"سلام {user_name} عزیز!\n"
        f"برای فعال شدن امکان ارسال پیام در «{chat_title}» باید هویت خود را تأیید کنی.\n\n"
        f"🧩 نوع چالش: {mode_label}\n"
        f"⏱ زمان باقی‌مانده: {to_persian_digits(str(timeout))} ثانیه\n"
        f"🎯 تلاش‌های باقی‌مانده: {to_persian_digits(str(max(0, attempts_left)))}\n\n"
        f"{getattr(session_record, 'payload_text', '') or ''}"
    ).strip()


def captcha_keyboard(session_record: CaptchaSession) -> InlineKeyboardMarkup:
    buttons = []
    for index, option in enumerate(list(session_record.options or [])[:4]):
        buttons.append(btn(str(option), f"cap:{session_record.token}:{index}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_captcha(bot: Bot, session: AsyncSession, *, chat_id: int, user, mode: str,
                        timeout: int, max_attempts: int, chat_title: str = "") -> CaptchaSession | None:
    """Restrict a new member and send the challenge."""
    from ..core.errors import safe_restrict

    await safe_restrict(bot, chat_id, user.id, MUTE_PERMISSIONS, context="captcha_restrict")
    record = await create_session(session, chat_id, user.id, mode=mode, timeout=timeout,
                                  max_attempts=max_attempts)
    state = await get_member_state(session, chat_id, user.id)
    state.captcha_passed = False
    state.updated_at = datetime.utcnow()
    invalidate_member(chat_id, user.id)

    text = captcha_text(record, user.first_name or "کاربر", chat_title or "گروه",
                        max_attempts, timeout)
    message = await safe_call(
        lambda: bot.send_message(chat_id=chat_id, text=text,
                                 reply_markup=captcha_keyboard(record)),
        context="captcha_send")
    if message is not None:
        record.message_id = message.message_id
        await session.flush()
    return record


async def solve(bot: Bot, session: AsyncSession, *, token: str, user_id: int,
                choice_index: int, chat_title: str = "") -> tuple[bool, str]:
    """Verify an answer. Returns ``(solved, message_for_user)``."""
    result = await session.execute(
        select(CaptchaSession).where(CaptchaSession.token == token)
    )
    record = result.scalar_one_or_none()
    if record is None:
        return False, "❌ این چالش دیگر معتبر نیست."
    if record.solved or record.failed:
        return True, "✅ این چالش قبلاً حل شده است."
    if record.user_id != user_id:
        return False, "⛔️ این چالش مربوط به شما نیست."
    if record.expires_at < datetime.utcnow():
        record.failed = True
        record.resolved_at = datetime.utcnow()
        await session.flush()
        await fail_action(bot, session, record, chat_title=chat_title, reason="اتمام زمان چالش")
        return False, "⌛️ زمان این چالش تمام شده است."

    options = list(record.options or [])
    chosen = options[choice_index] if 0 <= choice_index < len(options) else None
    correct = str(chosen) == str(record.answer)
    if correct:
        record.solved = True
        record.resolved_at = datetime.utcnow()
        from ..core.errors import safe_delete, safe_restrict

        await safe_restrict(bot, record.chat_id, record.user_id, UNMUTE_PERMISSIONS,
                            context="captcha_unmute")
        state = await get_member_state(session, record.chat_id, record.user_id)
        state.captcha_passed = True
        state.status = "member"
        state.updated_at = datetime.utcnow()
        invalidate_member(record.chat_id, record.user_id)
        await session.flush()
        if record.message_id:
            await safe_delete(bot, record.chat_id, record.message_id, context="captcha_cleanup")
        return True, "✅ تأیید انجام شد؛ اکنون می‌توانی در گروه پیام بفرستی."
    record.attempts = int(record.attempts or 0) + 1
    await session.flush()
    left = int(record.max_attempts or 3) - int(record.attempts or 0)
    if left <= 0:
        record.failed = True
        record.resolved_at = datetime.utcnow()
        await session.flush()
        await fail_action(bot, session, record, chat_title=chat_title, reason="تعداد تلاش‌های ناموفق")
        return False, "❌ تلاش‌های شما تمام شد و طبق تنظیمات گروه با شما برخورد شد."
    return False, f"❌ پاسخ نادرست بود. تلاش‌های باقی‌مانده: {to_persian_digits(str(left))}"


async def fail_action(bot: Bot, session: AsyncSession, record: CaptchaSession, *,
                      reason: str, chat_title: str = "") -> None:
    """Apply the configured failure action (kick / ban / mute)."""
    from .chat_state import get_settings
    from .moderation import apply_action
    from .permissions import get_bot_actor

    settings_obj = await get_settings(session, record.chat_id)
    action = (settings_obj.captcha_fail_action or "kick").strip().lower()
    actor = await get_bot_actor(bot, record.chat_id)
    await apply_action(bot, session, chat_id=record.chat_id, target_id=record.user_id,
                       actor=actor, action=action, reason=reason,
                       target_name=str(record.user_id), chat_title=chat_title,
                       source="captcha")
    if record.message_id:
        from ..core.errors import safe_delete

        await safe_delete(bot, record.chat_id, record.message_id, context="captcha_fail_cleanup")


async def expire_pending(bot: Bot, session: AsyncSession) -> int:
    """Timeout handler invoked by the scheduler."""
    now = datetime.utcnow()
    result = await session.execute(
        select(CaptchaSession).where(CaptchaSession.solved.is_(False),
                                     CaptchaSession.failed.is_(False),
                                     CaptchaSession.expires_at <= now)
    )
    count = 0
    for record in result.scalars().all():
        record.failed = True
        record.resolved_at = now
        count += 1
        try:
            await fail_action(bot, session, record, reason="اتمام زمان چالش")
        except Exception as exc:  # noqa: BLE001
            logger.warning("captcha expiry handling failed: %s", exc)
    if count:
        await session.flush()
    return count


async def pending_count(session: AsyncSession, chat_id: int) -> int:
    result = await session.execute(
        select(CaptchaSession).where(CaptchaSession.chat_id == chat_id,
                                     CaptchaSession.solved.is_(False),
                                     CaptchaSession.failed.is_(False))
    )
    return len(result.scalars().all())
