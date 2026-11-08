"""Internal role system (layer 2 of the permission model).

The internal role system can never *grant* a permission that Telegram itself
does not give to the caller: it is always combined with the real Telegram
administrator capabilities in :mod:`app.services.permissions`.
"""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    """Internal roles ordered from the weakest to the strongest."""

    MEMBER = 10
    TRUSTED = 20
    CLEANER = 30
    MUTER = 40
    HELPER = 50
    MODERATOR = 60
    ADMIN = 70
    SUPER_ADMIN = 80
    COFOUNDER = 90
    FOUNDER = 100


ROLE_NAMES_FA: dict[str, str] = {
    "member": "👤 عضو عادی",
    "trusted": "⭐️ کاربر ویژه",
    "cleaner": "🧹 پاکساز",
    "muter": "🔇 ساکت‌کننده",
    "helper": "🤝 کمک‌یار",
    "moderator": "🛡 ناظر",
    "admin": "👮 مدیر",
    "super_admin": "👑 مدیر ارشد",
    "cofounder": "💎 هم‌بنیان‌گذار",
    "founder": "🏆 مالک گروه",
}

ROLE_KEYS: tuple[str, ...] = tuple(
    name.lower() for name in ("MEMBER", "TRUSTED", "CLEANER", "MUTER", "HELPER",
                              "MODERATOR", "ADMIN", "SUPER_ADMIN", "COFOUNDER", "FOUNDER")
)

# Persian command fragments used when promoting / demoting staff.
ROLE_COMMAND_ALIASES: dict[str, str] = {
    "مدیر": "admin",
    "ناظر": "moderator",
    "پاکساز": "cleaner",
    "ساکت‌کننده": "muter",
    "ساکت کننده": "muter",
    "کمکیار": "helper",
    "کمک‌یار": "helper",
    "کمک يار": "helper",
    "ویژه": "trusted",
    "کاربر ویژه": "trusted",
    "مدیر ارشد": "super_admin",
    "همبنیانگذار": "cofounder",
    "هم‌بنیان‌گذار": "cofounder",
    "معاون": "cofounder",
}

# Roles that staff-management commands may assign, mapped to the minimum role
# required to assign them.
ASSIGNABLE_ROLES: dict[str, str] = {
    "cofounder": "founder",
    "super_admin": "cofounder",
    "moderator": "admin",
    "helper": "admin",
    "muter": "admin",
    "cleaner": "admin",
    "trusted": "admin",
}

LEVEL_BY_ROLE: dict[str, int] = {key: int(getattr(Role, key.upper())) for key in ROLE_KEYS}


def level_of(role: str | None) -> int:
    if not role:
        return int(Role.MEMBER)
    return LEVEL_BY_ROLE.get(role.lower().strip(), int(Role.MEMBER))


def role_name_fa(role: str | None, telegram_status: str | None = None) -> str:
    if telegram_status == "creator":
        return ROLE_NAMES_FA["founder"]
    if not role:
        return ROLE_NAMES_FA["member"]
    return ROLE_NAMES_FA.get(role.lower(), ROLE_NAMES_FA["member"])


def at_least(role: str | None, minimum: str) -> bool:
    return level_of(role) >= level_of(minimum)


def normalize_role_key(text: str | None) -> str | None:
    """Resolve a Persian (or internal english) role name to its role key."""
    if not text:
        return None
    from ..core.normalization import normalize_text

    raw = normalize_text(text, mode="command").strip()
    if not raw:
        return None
    if raw in LEVEL_BY_ROLE:
        return raw
    for alias, key in ROLE_COMMAND_ALIASES.items():
        if normalize_text(alias, mode="command") == raw:
            return key
        if raw.endswith(normalize_text(alias, mode="command")):
            return key
    return None


def can_manage_role(actor_role: str | None, target_role: str | None) -> bool:
    """An actor may only manage strictly weaker roles than their own."""
    actor_level = level_of(actor_role)
    target_level = level_of(target_role)
    if actor_level >= int(Role.FOUNDER):
        return actor_level > target_level or target_level < int(Role.COFOUNDER)
    return actor_level > target_level


def role_for_level(level: int) -> str | None:
    """Highest role name at or below ``level`` (used for permission checks)."""
    best: str | None = None
    best_level = -1
    for key in ROLE_KEYS:
        value = int(LEVEL_BY_ROLE.get(key, 0))
        if value <= level and value >= best_level:
            best, best_level = key, value
    return best
