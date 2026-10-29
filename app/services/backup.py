"""Group configuration backup / restore (JSON, validated, secret-free)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.normalization import to_persian_digits
from ..core.timeutils import persian_datetime
from ..db.models import (
    ChatSettings,
    FilterRule,
    Lock,
    ModerationAction,
    Note,
    PersonalCommand,
    ScheduledMessage,
    TrustedUser,
    BotRole,
    Warning,
)

logger = logging.getLogger("armando.backup")

FORMAT = "armando-backup"
VERSION = 1

# Columns that must never leave the database (identifiers of other groups,
# secrets, and internal bookkeeping).
EXCLUDED_SETTINGS = {"chat_id", "created_at", "updated_at"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    raise TypeError(f"not serializable: {type(value)!r}")


async def export_chat(session: AsyncSession, chat_id: int) -> dict[str, Any]:
    settings_obj = await session.get(ChatSettings, chat_id)
    locks = (await session.execute(select(Lock).where(Lock.chat_id == chat_id))).scalars().all()
    filters = (await session.execute(select(FilterRule).where(FilterRule.chat_id == chat_id))).scalars().all()
    notes = (await session.execute(select(Note).where(Note.chat_id == chat_id))).scalars().all()
    commands = (await session.execute(
        select(PersonalCommand).where(PersonalCommand.chat_id == chat_id))).scalars().all()
    warns = (await session.execute(select(Warning).where(Warning.chat_id == chat_id))).scalars().all()
    trusted = (await session.execute(
        select(TrustedUser).where(TrustedUser.chat_id == chat_id))).scalars().all()
    roles = (await session.execute(
        select(BotRole).where(BotRole.chat_id == chat_id))).scalars().all()
    scheduled = (await session.execute(
        select(ScheduledMessage).where(ScheduledMessage.chat_id == chat_id))).scalars().all()

    payload: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "chat_id": chat_id,
        "settings": {},
        "locks": [],
        "filters": [],
        "notes": [],
        "commands": [],
        "warnings": [],
        "trusted": [],
        "roles": [],
        "scheduled": [],
    }

    if settings_obj is not None:
        for column in ChatSettings.__table__.columns:  # type: ignore[attr-defined]
            if column.name in EXCLUDED_SETTINGS:
                continue
            payload["settings"][column.name] = getattr(settings_obj, column.name)

    for row in locks:
        payload["locks"].append({"key": row.key, "enabled": row.enabled, "action": row.action,
                                 "duration": row.duration, "extra": row.extra})
    for row in filters:
        payload["filters"].append({
            "trigger": row.trigger, "is_blocklist": row.is_blocklist, "match_mode": row.match_mode,
            "action": row.action, "duration": row.duration, "enabled": row.enabled,
            "response_text": row.response_text, "response_type": row.response_type,
            "response_file_id": row.response_file_id, "response_caption": row.response_caption,
            "response_buttons": row.response_buttons, "cooldown": row.cooldown,
            "aliases": row.aliases, "replies": row.replies,
        })
    for row in notes:
        payload["notes"].append({"name": row.name, "content_type": row.content_type, "text": row.text,
                                 "file_id": row.file_id, "caption": row.caption,
                                 "buttons": row.buttons, "aliases": row.aliases,
                                 "noformat": row.noformat})
    for row in commands:
        payload["commands"].append({"trigger": row.trigger, "aliases": row.aliases,
                                    "content_type": row.content_type, "text": row.text,
                                    "file_id": row.file_id, "caption": row.caption,
                                    "buttons": row.buttons, "cooldown": row.cooldown,
                                    "enabled": row.enabled})
    for row in warns:
        payload["warnings"].append({"user_id": row.user_id, "reason": row.reason,
                                    "active": row.active, "created_at": row.created_at.isoformat(),
                                    "moderator_id": row.moderator_id})
    for row in trusted:
        payload["trusted"].append({"user_id": row.user_id, "bypass": row.bypass})
    for row in roles:
        payload["roles"].append({"user_id": row.user_id, "role": row.role})
    for row in scheduled:
        payload["scheduled"].append({"name": row.name, "text": row.text, "media": row.media,
                                     "buttons": row.buttons, "repeat_seconds": row.repeat_seconds,
                                     "cron": row.cron, "timezone": row.timezone,
                                     "enabled": row.enabled})
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe)


def validate(payload: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "⛔️ فایل معتبر نیست."
    if payload.get("format") != FORMAT:
        return False, "⛔️ این فایل متعلق به Armando Group Manager نیست."
    if int(payload.get("version") or 0) > VERSION:
        return False, "⛔️ نسخه فایل پشتیبان جدیدتر از نسخه ربات است."
    for key in ("settings", "locks", "filters", "notes"):
        if key in payload and not isinstance(payload[key], (list, dict)):
            return False, f"⛔️ ساختار بخش «{key}» نامعتبر است."
    from .locks import LOCK_REGISTRY

    for lock in payload.get("locks", []):
        if not isinstance(lock, dict) or lock.get("key") not in LOCK_REGISTRY:
            return False, "⛔️ یکی از قفل‌های فایل ناشناخته است."
    return True, ""


async def import_chat(session: AsyncSession, chat_id: int, payload: dict[str, Any], *,
                      merge: bool = True) -> dict[str, int]:
    """Apply a validated backup to a chat. Returns per-section counters."""
    from .chat_state import ensure_chat, get_settings, invalidate_settings

    await ensure_chat(session, chat_id)
    from .filters import invalidate_filters
    from .locks import invalidate_locks

    ok, error = validate(payload)
    if not ok:
        raise ValueError(error)

    counters = {"settings": 0, "locks": 0, "filters": 0, "notes": 0, "commands": 0,
                "warnings": 0, "trusted": 0, "roles": 0, "scheduled": 0}

    settings_obj = await get_settings(session, chat_id)
    for key, value in (payload.get("settings") or {}).items():
        if key in EXCLUDED_SETTINGS or not hasattr(settings_obj, key):
            continue
        setattr(settings_obj, key, value)
        counters["settings"] += 1
    invalidate_settings(chat_id)

    from ..db.models import Lock as LockModel

    existing_locks = {row.key: row for row in
                      (await session.execute(select(LockModel).where(LockModel.chat_id == chat_id)))
                      .scalars().all()}
    for item in payload.get("locks") or []:
        from .locks import LOCK_REGISTRY

        if item.get("key") not in LOCK_REGISTRY:
            continue
        row = existing_locks.get(item["key"])
        if row is None:
            session.add(LockModel(chat_id=chat_id, key=item["key"], enabled=bool(item.get("enabled")),
                                  action=item.get("action") or "delete",
                                  duration=item.get("duration"), extra=item.get("extra") or {}))
        else:
            row.enabled = bool(item.get("enabled"))
            row.action = item.get("action") or row.action
            row.duration = item.get("duration")
            row.extra = item.get("extra") or {}
        counters["locks"] += 1
    invalidate_locks(chat_id)

    existing_filters = {row.trigger: row for row in
                        (await session.execute(select(FilterRule).where(FilterRule.chat_id == chat_id)))
                        .scalars().all()}
    for item in payload.get("filters") or []:
        trigger = (item.get("trigger") or "").strip()
        if not trigger:
            continue
        if trigger in existing_filters and merge:
            continue
        row = existing_filters.get(trigger) or FilterRule(chat_id=chat_id, trigger=trigger)
        row.is_blocklist = bool(item.get("is_blocklist"))
        row.match_mode = item.get("match_mode") or "word"
        row.action = item.get("action") or "delete"
        row.duration = item.get("duration")
        row.enabled = bool(item.get("enabled", True))
        row.response_text = item.get("response_text") or ""
        row.response_type = item.get("response_type") or "text"
        row.response_file_id = item.get("response_file_id")
        row.response_caption = item.get("response_caption")
        row.response_buttons = item.get("response_buttons") or []
        row.cooldown = int(item.get("cooldown") or 0)
        row.aliases = item.get("aliases") or []
        row.replies = item.get("replies") or []
        session.add(row)
        counters["filters"] += 1
    invalidate_filters(chat_id)

    existing_notes = {row.name: row for row in
                      (await session.execute(select(Note).where(Note.chat_id == chat_id)))
                      .scalars().all()}
    for item in payload.get("notes") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if name in existing_notes and merge:
            continue
        row = existing_notes.get(name) or Note(chat_id=chat_id, name=name)
        row.content_type = item.get("content_type") or "text"
        row.text = item.get("text") or ""
        row.file_id = item.get("file_id")
        row.caption = item.get("caption")
        row.buttons = item.get("buttons") or []
        row.aliases = item.get("aliases") or []
        row.noformat = bool(item.get("noformat"))
        session.add(row)
        counters["notes"] += 1

    existing_commands = {row.trigger: row for row in
                         (await session.execute(
                             select(PersonalCommand).where(PersonalCommand.chat_id == chat_id)))
                         .scalars().all()}
    for item in payload.get("commands") or []:
        trigger = (item.get("trigger") or "").strip()
        if not trigger:
            continue
        if trigger in existing_commands and merge:
            continue
        row = existing_commands.get(trigger) or PersonalCommand(chat_id=chat_id, trigger=trigger)
        row.content_type = item.get("content_type") or "text"
        row.text = item.get("text") or ""
        row.file_id = item.get("file_id")
        row.caption = item.get("caption")
        row.buttons = item.get("buttons") or []
        row.aliases = item.get("aliases") or []
        row.cooldown = int(item.get("cooldown") or 0)
        row.enabled = bool(item.get("enabled", True))
        session.add(row)
        counters["commands"] += 1

    existing_warns = {row.id for row in
                      (await session.execute(select(Warning).where(Warning.chat_id == chat_id)))
                      .scalars().all()}
    for item in payload.get("warnings") or []:
        if not item.get("user_id"):
            continue
        session.add(Warning(chat_id=chat_id, user_id=int(item["user_id"]),
                            moderator_id=item.get("moderator_id"),
                            reason=(item.get("reason") or "")[:500],
                            active=bool(item.get("active", True)),
                            created_at=_parse_dt(item.get("created_at")) or datetime.utcnow()))
        counters["warnings"] += 1
    del existing_warns

    existing_trusted = {row.user_id for row in
                        (await session.execute(
                            select(TrustedUser).where(TrustedUser.chat_id == chat_id))).scalars().all()}
    for item in payload.get("trusted") or []:
        user_id = int(item.get("user_id") or 0)
        if not user_id or user_id in existing_trusted:
            continue
        session.add(TrustedUser(chat_id=chat_id, user_id=user_id, bypass=item.get("bypass") or []))
        counters["trusted"] += 1

    existing_roles = {row.user_id for row in
                      (await session.execute(
                          select(BotRole).where(BotRole.chat_id == chat_id))).scalars().all()}
    for item in payload.get("roles") or []:
        user_id = int(item.get("user_id") or 0)
        if not user_id or user_id in existing_roles:
            continue
        session.add(BotRole(chat_id=chat_id, user_id=user_id, role=item.get("role") or "moderator"))
        counters["roles"] += 1

    for item in payload.get("scheduled") or []:
        session.add(ScheduledMessage(
            chat_id=chat_id, name=(item.get("name") or "")[:64], text=item.get("text") or "",
            media=item.get("media"), buttons=item.get("buttons") or [],
            repeat_seconds=item.get("repeat_seconds"), cron=item.get("cron"),
            timezone=item.get("timezone") or "Asia/Tehran", enabled=bool(item.get("enabled", True)),
            created_at=datetime.utcnow()))
        counters["scheduled"] += 1

    await session.flush()
    return counters


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def backup_summary(payload: dict[str, Any]) -> str:
    return "\n".join([
        "💾 <b>خلاصه فایل پشتیبان</b>",
        "",
        f"📦 فرمت: {payload.get('format', '—')}",
        f"🔢 نسخه: {to_persian_digits(str(payload.get('version', 0)))}",
        f"🕒 تاریخ تهیه: {persian_datetime(_parse_dt(payload.get('exported_at')))}",
        f"🔒 قفل‌ها: {to_persian_digits(str(len(payload.get('locks') or [])))}",
        f"🚫 فیلترها: {to_persian_digits(str(len(payload.get('filters') or [])))}",
        f"📝 یادداشت‌ها: {to_persian_digits(str(len(payload.get('notes') or [])))}",
        f"⌨️ دستورات شخصی: {to_persian_digits(str(len(payload.get('commands') or [])))}",
        f"⚠️ اخطارها: {to_persian_digits(str(len(payload.get('warnings') or [])))}",
        f"⭐️ کاربران ویژه: {to_persian_digits(str(len(payload.get('trusted') or [])))}",
        f"👮 نقش‌ها: {to_persian_digits(str(len(payload.get('roles') or [])))}",
        f"⏰ زمان‌بندی‌ها: {to_persian_digits(str(len(payload.get('scheduled') or [])))}",
    ])
