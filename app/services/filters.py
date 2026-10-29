"""Blacklist (blocklist) and custom trigger filters with anti-evasion matching."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import cache
from ..core.normalization import (
    collapse_letters,
    contains_word,
    normalize_text,
    similarity,
)
from ..db.models import FilterRule

logger = logging.getLogger("armando.filters")

MATCH_MODES_FA = {
    "word": "کلمه",
    "exact": "دقیق",
    "contains": "شامل",
    "regex": "الگو (regex)",
}

ACTIONS_FA = {
    "delete": "حذف پیام",
    "warn": "اخطار",
    "mute": "سکوت",
    "temp_mute": "سکوت موقت",
    "kick": "اخراج",
    "ban": "بن",
    "temp_ban": "بن موقت",
    "delete_warn": "حذف + اخطار",
    "delete_mute": "حذف + سکوت",
    "delete_ban": "حذف + بن",
    "delete_kick": "حذف + اخراج",
    "none": "بدون اقدام",
}


@dataclass
class RuleView:
    id: int
    trigger: str
    is_blocklist: bool
    match_mode: str
    action: str
    duration: int | None
    enabled: bool
    aliases: list[str] = field(default_factory=list)
    response_text: str = ""
    response_type: str = "text"
    # The media/UI half of a custom response.  These must mirror FilterRule,
    # otherwise the cached view cannot answer an auto-reply trigger at all.
    response_file_id: str | None = None
    response_caption: str | None = None
    response_buttons: list | None = None
    replies: list | None = None          # random response sets
    cooldown: int = 0
    hits: int = 0


def _to_view(rule: FilterRule) -> RuleView:
    return RuleView(
        id=rule.id,
        trigger=rule.trigger,
        is_blocklist=bool(rule.is_blocklist),
        match_mode=rule.match_mode,
        action=rule.action,
        duration=rule.duration,
        enabled=bool(rule.enabled),
        aliases=list(rule.aliases or []),
        response_text=rule.response_text or "",
        response_type=rule.response_type or "text",
        response_file_id=rule.response_file_id,
        response_caption=rule.response_caption,
        response_buttons=list(rule.response_buttons) if rule.response_buttons else None,
        replies=list(rule.replies) if rule.replies else None,
        cooldown=int(rule.cooldown or 0),
        hits=int(rule.hits or 0),
    )


async def get_rules(session: AsyncSession, chat_id: int) -> list[RuleView]:
    """Cached list of every rule of a chat."""
    cached = cache.filter_cache.get(chat_id)
    if cached is not None:
        return cached
    result = await session.execute(
        select(FilterRule).where(FilterRule.chat_id == chat_id).order_by(FilterRule.created_at.desc())
    )
    rules = [_to_view(r) for r in result.scalars().all()]
    cache.filter_cache.set(chat_id, rules)
    return rules


def invalidate_filters(chat_id: int) -> None:
    cache.filter_cache.delete(chat_id)


async def add_rule(session: AsyncSession, chat_id: int, trigger: str, *, is_blocklist: bool = False,
                   match_mode: str = "word", action: str = "delete", duration: int | None = None,
                   response_text: str = "", response_type: str = "text",
                   response_file_id: str | None = None, response_caption: str | None = None,
                   response_buttons: list | None = None, cooldown: int = 0,
                   aliases: list[str] | None = None, replies: list | None = None,
                   created_by: int | None = None) -> FilterRule:
    from .chat_state import ensure_chat

    trigger = (trigger or "").strip()
    if not trigger:
        raise ValueError("trigger is required")
    await ensure_chat(session, chat_id)
    result = await session.execute(
        select(FilterRule).where(FilterRule.chat_id == chat_id,
                                 FilterRule.trigger == normalize_text(trigger, mode="command"))
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise ValueError("duplicate")
    rule = FilterRule(
        chat_id=chat_id,
        trigger=normalize_text(trigger, mode="command"),
        is_blocklist=is_blocklist,
        match_mode=match_mode,
        action=action,
        duration=duration,
        response_text=response_text or "",
        response_type=response_type,
        response_file_id=response_file_id,
        response_caption=response_caption,
        response_buttons=response_buttons or [],
        cooldown=cooldown,
        aliases=[normalize_text(a, mode="command") for a in (aliases or [])],
        replies=replies or [],
        created_by=created_by,
        enabled=True,
    )
    session.add(rule)
    await session.flush()
    invalidate_filters(chat_id)
    return rule


async def remove_rule(session: AsyncSession, chat_id: int, trigger: str) -> bool:
    normalized = normalize_text(trigger, mode="command")
    result = await session.execute(
        select(FilterRule).where(FilterRule.chat_id == chat_id, FilterRule.trigger == normalized)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        return False
    await session.delete(rule)
    await session.flush()
    invalidate_filters(chat_id)
    return True


async def clear_rules(session: AsyncSession, chat_id: int, *, blocklist: bool = True,
                      custom: bool = True) -> int:
    keys = []
    if blocklist:
        keys.append(True)
    if custom:
        keys.append(False)
    if not keys:
        return 0
    result = await session.execute(
        delete(FilterRule).where(FilterRule.chat_id == chat_id, FilterRule.is_blocklist.in_(keys))
    )
    await session.flush()
    invalidate_filters(chat_id)
    return int(result.rowcount or 0)


async def bump_hits(session: AsyncSession, rule_id: int) -> None:
    rule = await session.get(FilterRule, rule_id)
    if rule is not None:
        rule.hits = int(rule.hits or 0) + 1


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _matches(trigger: str, text: str, mode: str) -> bool:
    trigger_norm = normalize_text(trigger, mode="command")
    text_norm = normalize_text(text, mode="command")
    if mode == "exact":
        return trigger_norm == text_norm
    if mode == "contains":
        return trigger_norm in text_norm
    if mode == "regex":
        try:
            return bool(re.search(trigger, text, flags=re.IGNORECASE | re.UNICODE))
        except re.error:
            return False
    # default: word mode with anti-evasion
    return contains_word(text, trigger)


def match_rule(text: str, rule: RuleView) -> bool:
    if not text:
        return False
    if _matches(rule.trigger, text, rule.match_mode):
        return True
    for alias in rule.aliases or []:
        if _matches(alias, text, rule.match_mode):
            return True
    if rule.match_mode == "word":
        # Anti-evasion: compare the "letters only" form too.
        tight_text = collapse_letters(text)
        for candidate in [rule.trigger, *(rule.aliases or [])]:
            tight_trigger = collapse_letters(candidate)
            if tight_trigger and tight_trigger in tight_text:
                return True
    return False


def find_blocklist_match(text: str, rules: list[RuleView]) -> RuleView | None:
    for rule in rules:
        if not rule.is_blocklist or not rule.enabled:
            continue
        if match_rule(text, rule):
            return rule
    return None


def find_custom_match(text: str, rules: list[RuleView]) -> RuleView | None:
    normalized = normalize_text(text or "", mode="command")
    for rule in rules:
        if rule.is_blocklist or not rule.enabled:
            continue
        if match_rule(text, rule):
            return rule
    # Loose similarity fallback for short messages (spacing tricks).
    for rule in rules:
        if rule.is_blocklist or not rule.enabled:
            continue
        if rule.match_mode == "exact" and similarity(normalized, rule.trigger) >= 0.92:
            return rule
    return None


def rules_text(rules: list[RuleView], *, blocklist: bool = True, title: str = "🚫 لیست فیلترها") -> str:
    selected = [r for r in rules if r.is_blocklist == blocklist]
    if not selected:
        return f"{title}\n\nلیست خالی است."
    lines = [title, ""]
    for index, rule in enumerate(selected, start=1):
        duration = f" | ⏱ {rule.duration // 60}د" if rule.duration else ""
        lines.append(
            f"{index}. <code>{rule.trigger}</code>\n"
            f"   ⚙️ {MATCH_MODES_FA.get(rule.match_mode, rule.match_mode)} | "
            f"🎯 {ACTIONS_FA.get(rule.action, rule.action)}{duration}"
        )
        if rule.aliases:
            lines.append(f"   🔁 هم‌معنی‌ها: {', '.join(rule.aliases[:5])}")
    return "\n".join(lines)
