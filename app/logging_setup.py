"""Structured logging with rotation (never logs secrets)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path

from .config import settings

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SECRET_PATTERNS = [
    re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}"),  # telegram bot token
    re.compile(r"sk-[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


class RedactingFilter(logging.Filter):
    """Remove anything that looks like a credential from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for pattern in SECRET_PATTERNS:
                message = pattern.sub("***", message)
            record.msg = message
            record.args = ()
        except Exception:  # noqa: BLE001
            return True
        return True


class JsonFormatter(logging.Formatter):
    """Compact JSON lines for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s",
                         "%Y-%m-%d %H:%M:%S")


def setup_logging(level: str | None = None, *, json_output: bool | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level_name = (level or settings.log_level or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level_name)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    use_json = settings.is_production if json_output is None else json_output
    formatter: logging.Formatter = JsonFormatter() if use_json else HumanFormatter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(RedactingFilter())
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "armando.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(RedactingFilter())
    root.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JsonFormatter())
    error_handler.addFilter(RedactingFilter())
    root.addHandler(error_handler)

    # Silence noisy third-party loggers
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.database_echo else logging.WARNING)

    logging.getLogger("armando").info(
        "logging ready level=%s json=%s", level_name, use_json)
