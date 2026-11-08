"""Reusable inline keyboard factory with Telegram button styles.

Buttons follow a consistent visual language:

* ``primary`` - normal / main action
* ``success`` - enable / positive action
* ``danger``  - destructive action
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"
DEFAULT = "default"

BACK_TEXT = "⬅️ بازگشت"
HOME_TEXT = "🏠 خانه"
CLOSE_TEXT = "❌ بستن"

SEPARATOR = ":"


def cb(*parts: Any) -> str:
    """Build a compact callback data string (Telegram limit: 64 bytes)."""
    cleaned = [str(p) for p in parts if p is not None and str(p) != ""]
    data = SEPARATOR.join(cleaned)
    if len(data.encode("utf-8")) > 64:  # defensive: keep it inside the API limit
        data = data.encode("utf-8")[:60].decode("utf-8", "ignore")
    return data


def parse_cb(data: str) -> list[str]:
    return (data or "").split(SEPARATOR)


def btn(text: str, callback_data: str | None = None, *, url: str | None = None,
        style: str | None = None, **kwargs) -> InlineKeyboardButton:
    payload: dict[str, Any] = {"text": text}
    if url:
        payload["url"] = url
    elif callback_data is not None:
        payload["callback_data"] = callback_data
        if style:
            payload["style"] = style
    payload.update(kwargs)
    return InlineKeyboardButton(**payload)


def primary(text: str, callback_data: str) -> InlineKeyboardButton:
    return btn(text, callback_data, style=PRIMARY)


def success(text: str, callback_data: str) -> InlineKeyboardButton:
    return btn(text, callback_data, style=SUCCESS)


def danger(text: str, callback_data: str) -> InlineKeyboardButton:
    return btn(text, callback_data, style=DANGER)


def onoff(text: str, enabled: bool, callback_data: str) -> InlineKeyboardButton:
    return btn(text, callback_data, style=SUCCESS if enabled else DANGER)


def back(target: str = "main") -> InlineKeyboardButton:
    return btn(BACK_TEXT, cb("nav", "back", target))


def home() -> InlineKeyboardButton:
    return btn(HOME_TEXT, cb("nav", "home"))


def close() -> InlineKeyboardButton:
    return btn(CLOSE_TEXT, cb("nav", "close"))


def row(*buttons: InlineKeyboardButton | None) -> list[InlineKeyboardButton]:
    return [b for b in buttons if b is not None]


def grid(buttons: Sequence[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(buttons), per_row):
        rows.append(list(buttons[index:index + per_row]))
    return rows


def markup(rows: Iterable[Sequence[InlineKeyboardButton] | InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """Build a keyboard from rows, dropping empty rows automatically."""
    final: list[list[InlineKeyboardButton]] = []
    for item in rows:
        if isinstance(item, InlineKeyboardButton):
            final.append([item])
            continue
        buttons = [b for b in item if b is not None]
        if buttons:
            final.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=final)


def nav_row(target: str = "main", *, with_home: bool = True) -> list[InlineKeyboardButton]:
    buttons = [back(target)]
    if with_home:
        buttons.append(home())
    return buttons


def confirm(yes_cb: str, no_cb: str, *, yes_text: str = "✅ بله", no_text: str = "❌ انصراف",
            target: str = "main") -> InlineKeyboardMarkup:
    return markup([
        row(success(yes_text, yes_cb), danger(no_text, no_cb)),
        nav_row(target),
    ])


def toggles(items: Sequence[tuple[str, bool, str]], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    """Build a grid of ON/OFF toggle buttons: ``(label, enabled, callback)``."""
    buttons = [onoff(f"{label}  {'🟢' if enabled else '🔴'}", enabled, callback_data)
               for label, enabled, callback_data in items]
    return grid(buttons, per_row=per_row)


def build_markup(spec: Any) -> InlineKeyboardMarkup | None:
    """Rebuild a keyboard from its JSON/serialized form (notes, welcome...)."""
    if not spec:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for raw_row in spec:
        if not isinstance(raw_row, (list, tuple)):
            continue
        buttons: list[InlineKeyboardButton] = []
        for raw_button in raw_row:
            if isinstance(raw_button, InlineKeyboardButton):
                buttons.append(raw_button)
                continue
            if not isinstance(raw_button, dict):
                continue
            text = str(raw_button.get("text") or "").strip()
            if not text:
                continue
            url = raw_button.get("url")
            callback_data = raw_button.get("callback_data")
            style = raw_button.get("style")
            if url:
                buttons.append(btn(text, url=url))
            elif callback_data:
                buttons.append(btn(text, callback_data, style=style))
        if buttons:
            rows.append(buttons)
    return markup(rows) if rows else None
