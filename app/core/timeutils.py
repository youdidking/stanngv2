"""Date / time helpers with first-class Jalali (Persian calendar) support.

The primary source is an offline computation (``jdatetime`` + ``zoneinfo``) so
the bot keeps working even when every external API is unreachable.  A remote
time API can optionally be used to keep the host clock honest.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

from ..config import settings
from .normalization import to_persian_digits

PERSIAN_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
PERSIAN_SEASONS = {1: "بهار", 2: "بهار", 3: "بهار", 4: "تابستان", 5: "تابستان",
                   6: "تابستان", 7: "پاییز", 8: "پاییز", 9: "پاییز",
                   10: "زمستان", 11: "زمستان", 12: "زمستان"}


def tzinfo(name: str | None = None):
    name = name or settings.timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Tehran")


def tehran_now() -> datetime:
    return datetime.now(tz=tzinfo())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def naive_utc(dt: datetime) -> datetime:
    """Return a naive UTC datetime (SQLite friendly)."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(tzinfo())
    return dt.astimezone(tzinfo())


def jalali(dt: datetime | None = None) -> jdatetime.datetime:
    dt = to_local(dt or tehran_now())
    return jdatetime.datetime.fromgregorian(datetime=dt)


def persian_date(dt: datetime | None = None, with_weekday: bool = True) -> str:
    jd = jalali(dt)
    text = f"{jd.day} {PERSIAN_MONTHS[jd.month - 1]} {jd.year}"
    if with_weekday:
        text = f"{PERSIAN_WEEKDAYS[jd.weekday()]} {text}"
    return to_persian_digits(text)


def persian_time(dt: datetime | None = None, with_seconds: bool = False) -> str:
    dt = to_local(dt or tehran_now())
    text = f"{dt.hour:02d}:{dt.minute:02d}"
    if with_seconds:
        text += f":{dt.second:02d}"
    return to_persian_digits(text)


def persian_datetime(dt: datetime | None = None, with_seconds: bool = False) -> str:
    dt = dt or tehran_now()
    return f"{persian_date(dt)} - ساعت {persian_time(dt, with_seconds)}"


def season_name(dt: datetime | None = None) -> str:
    return PERSIAN_SEASONS[jalali(dt).month]


GREGORIAN_MONTHS = [
    "ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن",
    "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر",
]
GREGORIAN_WEEKDAYS = [
    "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه",
]


def jalali_month_days(year: int, month: int) -> int:
    """Exact length of a Jalali month, derived from jdatetime conversions."""
    if month == 12:
        next_month = jdatetime.date(year + 1, 1, 1).togregorian()
    else:
        next_month = jdatetime.date(year, month + 1, 1).togregorian()
    first_day = jdatetime.date(year, month, 1).togregorian()
    return (next_month - first_day).days


def _calendar_values(dt: datetime | None = None) -> tuple[datetime, jdatetime.datetime, int, int, int]:
    local = to_local(dt or tehran_now())
    jd = jalali(local)
    first_year = jdatetime.date(jd.year, 1, 1).togregorian()
    first_month = jdatetime.date(jd.year, jd.month, 1).togregorian()
    first_next_year = jdatetime.date(jd.year + 1, 1, 1).togregorian()
    day_of_year = (first_month - first_year).days + jd.day
    days_in_year = (first_next_year - first_year).days
    days_in_month = jalali_month_days(jd.year, jd.month)
    return local, jd, day_of_year, days_in_year, days_in_month


def utc_offset_label(dt: datetime) -> str:
    """Format the actual timezone offset at this instant (DST-aware)."""
    offset = dt.utcoffset()
    if offset is None:
        return "UTC±00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "−"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def timezone_display_name(dt: datetime) -> str:
    """Persian name for common zones; retain the IANA identifier otherwise."""
    zone = getattr(dt.tzinfo, "key", None) or settings.timezone or "Asia/Tehran"
    known = {
        "Asia/Tehran": "ایران (تهران)",
        "America/Chicago": "ایالات متحده (شیکاگو)",
        "America/New_York": "ایالات متحده (نیویورک)",
        "America/Los_Angeles": "ایالات متحده (لس‌آنجلس)",
        "Europe/London": "بریتانیا (لندن)",
        "Europe/Paris": "فرانسه (پاریس)",
        "UTC": "ساعت هماهنگ جهانی",
        "Etc/UTC": "ساعت هماهنگ جهانی",
    }
    return known.get(zone, zone)


def date_details_text(dt: datetime | None = None) -> str:
    """Organized Jalali/Gregorian date details from one consistent local instant."""
    local, jd, day_of_year, days_in_year, days_in_month = _calendar_values(dt)
    gregorian = local.date()
    jalali_weekday = PERSIAN_WEEKDAYS[jd.weekday()]
    gregorian_weekday = GREGORIAN_WEEKDAYS[gregorian.weekday()]
    jalali_days_left = days_in_year - day_of_year
    jalali_leap = "کبیسه" if days_in_year == 366 else "عادی"
    gregorian_leap = "کبیسه" if calendar.isleap(gregorian.year) else "عادی"
    return "\n".join([
        "📅 <b>تاریخ امروز</b>",
        "",
        "🇮🇷 <b>هجری شمسی</b>",
        f"• تاریخ: {persian_date(local, with_weekday=False)}",
        f"• روز هفته: {jalali_weekday}",
        f"• روز ماه: {to_persian_digits(str(jd.day))} از {to_persian_digits(str(days_in_month))}",
        f"• روز سال: {to_persian_digits(str(day_of_year))} از {to_persian_digits(str(days_in_year))}",
        f"• مانده تا پایان سال: {to_persian_digits(str(jalali_days_left))} روز",
        f"• فصل: {season_name(local)}؛ سال {jalali_leap}",
        "",
        "🌍 <b>میلادی</b>",
        f"• تاریخ: {gregorian_weekday} {to_persian_digits(str(gregorian.day))} "
        f"{GREGORIAN_MONTHS[gregorian.month - 1]} {to_persian_digits(str(gregorian.year))}",
        f"• قالب استاندارد: {to_persian_digits(gregorian.isoformat())}",
        f"• هفتهٔ استاندارد ISO: {to_persian_digits(str(gregorian.isocalendar().week))}",
        f"• سال {gregorian_leap}",
    ])


def time_details_text(dt: datetime | None = None) -> str:
    """Show local wall-clock time, IANA zone/offset and the UTC equivalent."""
    local, _jd, _day_of_year, _days_in_year, _days_in_month = _calendar_values(dt)
    utc = local.astimezone(timezone.utc)
    zone_name = timezone_display_name(local)
    offset = utc_offset_label(local)
    return "\n".join([
        "🕒 <b>ساعت و منطقهٔ زمانی</b>",
        "",
        f"• ساعت محلی: {persian_time(local, with_seconds=True)}",
        f"• منطقهٔ زمانی: {zone_name} ({offset})",
        f"• تاریخ شمسی: {persian_date(local)}",
        f"• تاریخ میلادی: {to_persian_digits(local.date().isoformat())}",
        f"• زمان جهانی: {to_persian_digits(utc.strftime('%Y-%m-%d %H:%M:%S'))} UTC",
    ])


def calendar_details_text(dt: datetime | None = None) -> str:
    """Full, categorized current calendar and clock summary."""
    local = to_local(dt or tehran_now())
    return "🗓 <b>تقویم کامل</b>\n\n" + date_details_text(local) + "\n\n" + time_details_text(local)


def persian_relative(dt: datetime | None) -> str:
    """«۳ ساعت پیش» style formatting."""
    if not dt:
        return "نامشخص"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = utc_now() - dt.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return to_persian_digits(persian_datetime(dt))
    if seconds < 60:
        return to_persian_digits(f"{seconds} ثانیه پیش")
    if seconds < 3600:
        return to_persian_digits(f"{seconds // 60} دقیقه پیش")
    if seconds < 86400:
        return to_persian_digits(f"{seconds // 3600} ساعت پیش")
    if seconds < 2592000:
        return to_persian_digits(f"{seconds // 86400} روز پیش")
    return to_persian_digits(persian_date(dt))


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """Parse ``23:30`` / ``۲۳:۳۰`` into ``(hour, minute)``."""
    from .normalization import normalize_digits

    raw = normalize_digits(text or "", to="ascii").strip()
    match = None
    for sep in (":", ".", "-", " "):
        if sep in raw:
            match = raw.split(sep, 1)
            break
    if not match:
        if len(raw) == 4 and raw.isdigit():
            match = [raw[:2], raw[2:]]
        else:
            return None
    try:
        hour = int(match[0].strip())
        minute = int(match[1].strip())
    except (ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def next_occurrence(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """Next local datetime matching ``hour:minute`` (always in the future)."""
    now = to_local(now or tehran_now())
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
