"""Persian duration parsing and formatting.

Supported inputs (Persian and internal short aliases)::

    ۳۰دقیقه   30m   ۲ساعت   2h   ۳روز   3d   ۲هفته   2w   ۱ماه  30d
    ۱۰ ثانیه  10s
"""

from __future__ import annotations

import re

from .normalization import normalize_digits

SECOND = 1
MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
MONTH = 30 * DAY

UNIT_ALIASES: dict[str, int] = {
    # Persian (normalized: yeh/keh folded, digits ascii)
    "ثانيه": SECOND, "ثانیه": SECOND, "ثانيهها": SECOND, "ثانیهها": SECOND, "ثانييه": SECOND,
    "سانىيه": SECOND, "سانيه": SECOND, "سانييه": SECOND, "ثاني": SECOND,
    "دقیقه": MINUTE, "دقيقه": MINUTE, "دقیق": MINUTE, "دقيقت": MINUTE, "دقیقهاي": MINUTE,
    "دقیقهها": MINUTE, "دقیقهای": MINUTE, "دقيقهها": MINUTE, "دقیقم": MINUTE, "دقیقام": MINUTE,
    "ساعت": HOUR, "ساعتها": HOUR, "ساعتهاي": HOUR, "ساعتا": HOUR,
    "روز": DAY, "روزها": DAY, "روزم": DAY, "روزار": DAY,
    "هفته": WEEK, "هفتهها": WEEK, "هفتها": WEEK, "هفتهای": WEEK,
    "ماه": MONTH, "ماهها": MONTH,
    # Internal short aliases (never advertised in the Persian UI)
    "s": SECOND, "sec": SECOND, "secs": SECOND, "second": SECOND, "seconds": SECOND,
    "m": MINUTE, "min": MINUTE, "mins": MINUTE, "minute": MINUTE, "minutes": MINUTE,
    "h": HOUR, "hr": HOUR, "hrs": HOUR, "hour": HOUR, "hours": HOUR,
    "d": DAY, "day": DAY, "days": DAY,
    "w": WEEK, "week": WEEK, "weeks": WEEK,
    "mo": MONTH, "month": MONTH, "months": MONTH,
}

DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z\u0600-\u06ff]+)")


def parse_duration(text: str | None) -> int | None:
    """Parse a duration expression and return seconds (``None`` on failure)."""
    if not text:
        return None
    raw = normalize_digits(str(text), to="ascii").strip().lower()
    if not raw:
        return None
    # Fully numeric input is treated as minutes (bot-owner convenience).
    if raw.isdigit():
        return int(raw) * MINUTE
    total = 0
    matched = False
    for value, unit in DURATION_RE.findall(raw):
        seconds = UNIT_ALIASES.get(unit) or UNIT_ALIASES.get(unit.rstrip("ها"))
        if not seconds:
            # Try prefix matching for plural / dialect variants.
            for alias, mult in UNIT_ALIASES.items():
                if unit.startswith(alias[:3]) and len(alias) >= 3 and alias[0] == unit[0]:
                    seconds = mult
                    break
        if not seconds:
            continue
        try:
            total += int(float(value) * seconds)
            matched = True
        except ValueError:
            continue
    if not matched:
        return None
    return max(30, min(total, 366 * DAY))


def format_duration(seconds: int | None) -> str:
    """Return a Persian human readable duration."""
    if not seconds or seconds <= 0:
        return "♾ همیشگی"
    seconds = int(seconds)
    days, rem = divmod(seconds, DAY)
    hours, rem = divmod(rem, HOUR)
    minutes, secs = divmod(rem, MINUTE)
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    if secs and not days:
        parts.append(f"{secs} ثانیه")
    return " و ".join(parts[:3]) or "کمتر از یک دقیقه"


PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
