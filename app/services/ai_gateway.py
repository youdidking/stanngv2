"""Generic OpenAI-compatible model discovery and chat transport.

Model IDs and display names are read from the authenticated live catalog; no
model roster is embedded in the application.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .. import config
from .market import get_http_session

logger = logging.getLogger("armando.ai_gateway")

_CACHE_TTL_SECONDS = 300
_CACHE_LOCK = asyncio.Lock()
_cached_models: list["AIModel"] = []
_cached_at = 0.0


class AIGatewayError(RuntimeError):
    """Safe, user-facing-neutral error; never contains credentials or prompts."""


class AIGatewayNotConfigured(AIGatewayError):
    """The service has no API key configured."""


@dataclass(frozen=True)
class AIModel:
    id: str
    name: str


def model_ref(model_id: str) -> str:
    """Short stable reference suitable for Telegram's 64-byte callback limit."""
    return hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]


def _base_url() -> str:
    """Normalize an OpenAI-compatible API origin for raw HTTP requests.

    SDK examples often accept the bare host and add ``/v1`` internally. This
    service sends HTTP requests directly, so add ``/v1`` when the configured
    URL is only an origin. Explicit API prefixes (for example ``/v1`` or
    ``/openai/v1``) are preserved as supplied.
    """
    base = (config.settings.ai_chat_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    from urllib.parse import urlsplit

    parsed = urlsplit(base)
    if (parsed.scheme in {"http", "https"} and parsed.netloc
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment):
        return f"{base}/v1"
    return base


def _model_from_api(raw: Any) -> AIModel | None:
    """Take only the model ID and display name supplied by the provider."""
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id") or raw.get("model") or raw.get("model_id")
    if not isinstance(model_id, str):
        return None
    model_id = model_id.strip()
    if not model_id or len(model_id) > 200:
        return None
    name = raw.get("name") or raw.get("display_name") or raw.get("displayName") or model_id
    name = str(name).strip()[:200] or model_id
    return AIModel(id=model_id, name=name)


def _model_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "items", "results"):
        items = payload.get(key)
        if isinstance(items, list):
            return items
        if isinstance(items, dict):
            nested = items.get("data") or items.get("models")
            if isinstance(nested, list):
                return nested
    return []


def _safe_error_detail(payload: Any, api_key: str) -> str:
    """Extract a short provider diagnostic while redacting credentials."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        fields = [error.get(name) for name in ("code", "type", "message")]
    elif isinstance(error, str):
        fields = [error]
    else:
        fields = [payload.get(name) for name in ("code", "type", "message")]
    detail = " | ".join(str(value) for value in fields if isinstance(value, (str, int, float)))
    detail = " ".join(detail.split())
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    detail = re.sub(r"(?i)\b(?:sk-|tr_)[a-z0-9_-]{8,}\b", "[REDACTED]", detail)
    return detail[:300]


async def _response_error_detail(response: Any, api_key: str) -> str:
    try:
        payload = await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - diagnostic data is optional
        payload = None
    detail = _safe_error_detail(payload, api_key)
    if detail:
        return detail
    reason = " ".join(str(getattr(response, "reason", "") or "").split())
    if api_key:
        reason = reason.replace(api_key, "[REDACTED]")
    return reason[:160]


async def list_models(*, force_refresh: bool = False) -> list[AIModel]:
    """Fetch the models allowed by this API key and cache them for five minutes."""
    global _cached_models, _cached_at
    api_key = (config.settings.ai_chat_api_key or "").strip()
    base_url = _base_url()
    if not api_key or not base_url:
        raise AIGatewayNotConfigured("AI_CHAT_API_KEY and AI_CHAT_BASE_URL are required")
    now = time.monotonic()
    if not force_refresh and _cached_models and now - _cached_at < _CACHE_TTL_SECONDS:
        return list(_cached_models)

    async with _CACHE_LOCK:
        now = time.monotonic()
        if not force_refresh and _cached_models and now - _cached_at < _CACHE_TTL_SECONDS:
            return list(_cached_models)
        try:
            import aiohttp

            session = await get_http_session()
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.get(
                f"{_base_url()}/models",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=timeout,
            ) as response:
                if response.status >= 400:
                    detail = await _response_error_detail(response, api_key)
                    suffix = f" ({detail})" if detail else ""
                    logger.warning("AI provider model catalog returned HTTP %s%s", response.status, suffix)
                    raise AIGatewayError(f"model catalog HTTP {response.status}")
                payload = await response.json(content_type=None)
        except AIGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/API errors must stay local
            logger.warning("AI provider model catalog unavailable (%s)", exc.__class__.__name__)
            raise AIGatewayError("model catalog unavailable") from None

        models: list[AIModel] = []
        seen: set[str] = set()
        for raw in _model_items(payload):
            model = _model_from_api(raw)
            if model is None or model.id in seen:
                continue
            seen.add(model.id)
            models.append(model)
        if not models:
            logger.warning("AI provider model catalog returned no valid models")
            raise AIGatewayError("empty model catalog")
        _cached_models = models
        _cached_at = time.monotonic()
        return list(_cached_models)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "".join(chunks).strip()
    return ""


async def chat_completion(model: str, messages: list[dict[str, str]], *,
                          max_tokens: int = 1200) -> str:
    """Submit an OpenAI-compatible chat completion and return its text content."""
    api_key = (config.settings.ai_chat_api_key or "").strip()
    if not api_key or not _base_url():
        raise AIGatewayNotConfigured("AI_CHAT_API_KEY and AI_CHAT_BASE_URL are required")
    if not model or len(model) > 200:
        raise AIGatewayError("invalid model id")
    if not messages:
        raise AIGatewayError("no messages supplied")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max(1, min(int(max_tokens), 1600)),
        "stream": False,
    }
    try:
        import aiohttp

        session = await get_http_session()
        timeout = aiohttp.ClientTimeout(total=45)
        async with session.post(
            f"{_base_url()}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        ) as response:
            if response.status >= 400:
                detail = await _response_error_detail(response, api_key)
                suffix = f" ({detail})" if detail else ""
                logger.warning("AI provider completion returned HTTP %s%s", response.status, suffix)
                raise AIGatewayError(f"completion HTTP {response.status}")
            data = await response.json(content_type=None)
    except AIGatewayError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not leak request or credential data
        logger.warning("AI provider completion unavailable (%s)", exc.__class__.__name__)
        raise AIGatewayError("completion unavailable") from None

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise AIGatewayError("empty completion")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    answer = _content_text(message.get("content")) if isinstance(message, dict) else ""
    if not answer:
        raise AIGatewayError("completion contained no text")
    return answer


def reset_model_cache() -> None:
    """Clear cached catalog (used by tests and configuration reloads)."""
    global _cached_models, _cached_at
    _cached_models = []
    _cached_at = 0.0
