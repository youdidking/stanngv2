"""Currency, gold, crypto and stock-market prices from free APIs.

Providers (all free, no API key required):

* **TGJU** (``call1.tgju.org``) - Iranian free market: currencies, gold, crypto
  and the Tehran stock exchange index.  Prices are published in Rials.
* **CoinGecko** - global crypto prices in USD.
* **open.er-api.com** - official fiat reference rates.
* **Nobitex / Wallex** - optional Iranian exchanges (used when reachable).

Every provider is optional: if one fails, the next one is tried, and if all of
them fail the module reports the failure gracefully instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..config import settings
from ..core import cache
from ..core.normalization import normalize_text, to_persian_digits
from ..core.timeutils import persian_datetime

logger = logging.getLogger("armando.market")

TGJU_URL = "https://call1.tgju.org/ajax.json"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
ERAPI_URL = "https://open.er-api.com/v6/latest/USD"

_session: Any = None
_session_lock = asyncio.Lock()


async def get_http_session():
    """Shared aiohttp session (created on demand, closed on shutdown)."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                import aiohttp

                timeout = aiohttp.ClientTimeout(total=12)
                _session = aiohttp.ClientSession(timeout=timeout,
                                                 headers={"User-Agent": "ArmandoGroupManager/1.0"})
    return _session


async def close_http_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    key: str
    label: str
    price: float
    unit: str  # toman | usd | point
    change: float = 0.0
    source: str = ""
    updated: str = ""
    high: float | None = None
    low: float | None = None

    @property
    def price_text(self) -> str:
        if self.unit == "usd":
            return f"${self.price:,.2f}"
        if self.unit == "point":
            return to_persian_digits(f"{self.price:,.0f}")
        return to_persian_digits(f"{self.price:,.0f}") + " تومان"

    @property
    def change_text(self) -> str:
        if not self.change:
            return "—"
        sign = "🔺" if self.change > 0 else "🔻"
        return f"{sign} {to_persian_digits(f'{abs(self.change):.2f}')}٪"


CURRENCIES: dict[str, tuple[str, str]] = {
    # internal key: (Persian label, tgju key)
    "usd": ("دلار آمریکا", "price_dollar_rl"),
    "eur": ("یورو", "price_eur"),
    "gbp": ("پوند انگلیس", "price_gbp"),
    "try": ("لیر ترکیه", "price_try"),
    "aed": ("درهم امارات", "price_aed"),
    "sar": ("ریال عربستان", "price_sar"),
    "iqd": ("دینار عراق", "price_iqd"),
    "afn": ("افغانی", "price_afn"),
    "cad": ("دلار کانادا", "price_cad"),
    "aud": ("دلار استرالیا", "price_aud"),
    "chf": ("فرانک سوئیس", "price_chf"),
    "cny": ("یوان چین", "price_cny"),
    "inr": ("روپیه هند", "price_inr"),
    "rub": ("روبل روسیه", "price_rub"),
    "sek": ("کرون سوئد", "price_sek"),
    "kwd": ("دینار کویت", "price_kwd"),
    "azn": ("منات آذربایجان", "price_azn"),
    "amd": ("درام ارمنستان", "price_amd"),
    "thb": ("بات تایلند", "price_thb"),
    "myr": ("رینگیت مالزی", "price_myr"),
}

# Flags make the issuing country/region immediately recognizable in every
# currency quote. The euro is a shared currency, so its regional EU flag is used.
CURRENCY_FLAGS: dict[str, str] = {
    "usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "try": "🇹🇷",
    "aed": "🇦🇪", "sar": "🇸🇦", "iqd": "🇮🇶", "afn": "🇦🇫",
    "cad": "🇨🇦", "aud": "🇦🇺", "chf": "🇨🇭", "cny": "🇨🇳",
    "inr": "🇮🇳", "rub": "🇷🇺", "sek": "🇸🇪", "kwd": "🇰🇼",
    "azn": "🇦🇿", "amd": "🇦🇲", "thb": "🇹🇭", "myr": "🇲🇾",
}
CURRENCY_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("🌎 آمریکا و اقیانوسیه", ("usd", "cad", "aud")),
    ("🇪🇺 اروپا", ("eur", "gbp", "chf", "rub", "sek")),
    ("🌍 خاورمیانه و همسایگان", ("try", "aed", "sar", "iqd", "afn", "kwd", "azn", "amd")),
    ("🌏 آسیا", ("cny", "inr", "thb", "myr")),
)


def currency_display_name(key: str) -> str:
    label = CURRENCIES[key][0]
    return f"{CURRENCY_FLAGS.get(key, '💱')} {label}"


CURRENCY_ALIASES: dict[str, str] = {
    "دلار": "usd", "دلار آمریکا": "usd", "آمریکا": "usd", "usd": "usd", "دلارامریکا": "usd",
    "یورو": "eur", "eur": "eur", "اروپا": "eur",
    "پوند": "gbp", "gbp": "gbp", "پوند انگلیس": "gbp", "انگلیس": "gbp",
    "لیر": "try", "لیر ترکیه": "try", "ترکیه": "try", "try": "try",
    "درهم": "aed", "درهم امارات": "aed", "امارات": "aed", "aed": "aed", "دبی": "aed",
    "ریال عربستان": "sar", "عربستان": "sar", "sar": "sar",
    "دینار عراق": "iqd", "دینار": "iqd", "عراق": "iqd", "iqd": "iqd",
    "افغانی": "afn", "افغانستان": "afn", "afn": "afn",
    "دلار کانادا": "cad", "کانادا": "cad", "cad": "cad",
    "دلار استرالیا": "aud", "استرالیا": "aud", "aud": "aud",
    "فرانک": "chf", "سوئیس": "chf", "chf": "chf",
    "یوان": "cny", "چین": "cny", "cny": "cny",
    "روپیه": "inr", "هند": "inr", "inr": "inr",
    "روبل": "rub", "روسیه": "rub", "rub": "rub",
    "کرون": "sek", "سوئد": "sek",
    "کویت": "kwd", "منات": "azn", "ارمنستان": "amd", "تایلند": "thb", "مالزی": "myr",
}

CRYPTO: dict[str, tuple[str, str, str]] = {
    # internal key: (Persian label, tgju key, coingecko id)
    "btc": ("بیت‌کوین", "crypto-bitcoin", "bitcoin"),
    "eth": ("اتریوم", "crypto-ethereum", "ethereum"),
    "usdt": ("تتر", "crypto-tether", "tether"),
    "ton": ("تون‌کوین", "crypto-toncoin", "the-open-network"),
    "trx": ("ترون", "crypto-tron", "tron"),
    "sol": ("سولانا", "crypto-solana", "solana"),
    "doge": ("دوج‌کوین", "crypto-dogecoin", "dogecoin"),
    "shib": ("شیبا اینو", "crypto-shiba-inu", "shiba-inu"),
    "xrp": ("ریپل", "crypto-ripple", "ripple"),
    "ada": ("کاردانو", "crypto-cardano", "cardano"),
    "bnb": ("بایننس‌کوین", "crypto-binance-coin", "binancecoin"),
    "ltc": ("لایت‌کوین", "crypto-litecoin", "litecoin"),
    "dot": ("پولکادات", "crypto-polkadot", "polkadot"),
    "avax": ("آوالانچ", "crypto-avalanche", "avalanche-2"),
    "link": ("چین‌لینک", "crypto-chainlink", "chainlink"),
    "bch": ("بیت‌کوین‌کش", "crypto-bitcoin-cash", "bitcoin-cash"),
    "xlm": ("استلار", "crypto-stellar", "stellar"),
    "xtz": ("تزوس", "crypto-tezos", "tezos"),
    "xmr": ("مونرو", "crypto-monero", "monero"),
    "uni": ("یونی‌سواپ", "crypto-uniswap", "uniswap"),
    "etc": ("اتریوم کلاسیک", "crypto-ethereum-classic", "ethereum-classic"),
    "eos": ("ایاس", "crypto-eos", "eos"),
    "fil": ("فایل‌کوین", "crypto-filecoin", "filecoin"),
    "usdc": ("یو‌اس‌دی‌سی", "crypto-usd-coin", "usd-coin"),
}

CRYPTO_ALIASES: dict[str, str] = {
    "بیتکوین": "btc", "بیت کوین": "btc", "btc": "btc", "bitcoin": "btc", "بیت‌کوین": "btc",
    "اتریوم": "eth", "eth": "eth", "ethereum": "eth",
    "تتر": "usdt", "usdt": "usdt", "tether": "usdt",
    "تون": "ton", "تون کوین": "ton", "تون‌کوین": "ton", "toncoin": "ton", "ton": "ton",
    "ترون": "trx", "trx": "trx", "tron": "trx",
    "سولانا": "sol", "sol": "sol", "solana": "sol",
    "دوج": "doge", "دوج کوین": "doge", "دوج‌کوین": "doge", "doge": "doge", "dogecoin": "doge",
    "شیبا": "shib", "شیبا اینو": "shib", "shib": "shib", "shiba": "shib",
    "ریپل": "xrp", "xrp": "xrp", "ripple": "xrp",
    "کاردانو": "ada", "ada": "ada", "cardano": "ada",
    "بایننس": "bnb", "bnb": "bnb", "binance": "bnb",
    "لایت کوین": "ltc", "لایتکوین": "ltc", "ltc": "ltc",
    "پولکادات": "dot", "dot": "dot", "polkadot": "dot",
    "آوالانچ": "avax", "avax": "avax", "avalanche": "avax",
    "چین لینک": "link", "چین‌لینک": "link", "link": "link", "chainlink": "link",
    "بیت کوین کش": "bch", "bch": "bch",
    "استلار": "xlm", "xlm": "xlm", "stellar": "xlm",
    "تزوس": "xtz", "xtz": "xtz", "مونرو": "xmr", "xmr": "xmr", "monero": "xmr",
    "یونی سواپ": "uni", "uni": "uni", "uniswap": "uni",
    "فایل کوین": "fil", "fil": "fil", "یو اس دی سی": "usdc", "usdc": "usdc",
}

GOLD: dict[str, tuple[str, str, str]] = {
    # key: (label, tgju key, unit)
    "coin_emami": ("سکه امامی", "sekee", "toman"),
    "coin_bahar": ("سکه بهار آزادی", "sekeb", "toman"),
    "gram18": ("طلای ۱۸ عیار (گرم)", "geram18", "toman"),
    "mesghal": ("مثقال طلا", "mesghal", "toman"),
    "ounce": ("انس جهانی طلا", "ons", "usd"),
}

GOLD_ALIASES: dict[str, str] = {
    "سکه": "coin_emami", "سکه امامی": "coin_emami", "امامی": "coin_emami", "سکه امامی​": "coin_emami",
    "بهار آزادی": "coin_bahar", "بهار": "coin_bahar", "سکه بهار": "coin_bahar",
    "طلای 18 عیار": "gram18", "طلای ۱۸ عیار": "gram18", "گرم طلا": "gram18", "طلای آبشده": "mesghal",
    "مثقال": "mesghal", "مثقال طلا": "mesghal", "آبشده": "mesghal",
    "انس": "ounce", "انس طلا": "ounce", "اونس": "ounce", "اونس طلا": "ounce", "انس جهانی": "ounce",
}


def resolve_currency(text: str) -> str | None:
    raw = normalize_text(text or "", mode="command").strip()
    if not raw:
        return None
    if raw in CURRENCIES:
        return raw
    aliases = {normalize_text(k, mode="command"): v for k, v in CURRENCY_ALIASES.items()}
    return aliases.get(raw)


def resolve_crypto(text: str) -> str | None:
    raw = normalize_text(text or "", mode="command").strip()
    if not raw:
        return None
    if raw in CRYPTO:
        return raw
    aliases = {normalize_text(k, mode="command"): v for k, v in CRYPTO_ALIASES.items()}
    return aliases.get(raw)


def resolve_gold(text: str) -> str | None:
    raw = normalize_text(text or "", mode="command").strip()
    if not raw:
        return None
    if raw in GOLD:
        return raw
    aliases = {normalize_text(k, mode="command"): v for k, v in GOLD_ALIASES.items()}
    return aliases.get(raw)


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


async def _get_json(url: str, params: dict | None = None) -> dict | None:
    try:
        session = await get_http_session()
        async with session.get(url, params=params) as response:
            if response.status != 200:
                logger.debug("provider %s returned %s", url, response.status)
                return None
            return await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - provider failures must not crash
        logger.debug("provider %s failed: %s", url, exc)
        return None


async def fetch_tgju() -> dict[str, Any]:
    cached = cache.market_cache.get("tgju")
    if cached is not None:
        return cached
    data = await _get_json(TGJU_URL)
    if not isinstance(data, dict):
        return {}
    current = data.get("current") or {}
    cache.market_cache.set("tgju", current, ttl=float(settings.market_cache_ttl))
    return current


async def fetch_coingecko(ids: list[str]) -> dict[str, Any]:
    key = f"cg:{','.join(sorted(ids))}"
    cached = cache.market_cache.get(key)
    if cached is not None:
        return cached
    data = await _get_json(COINGECKO_URL, params={
        "ids": ",".join(sorted(set(ids))),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    })
    data = data if isinstance(data, dict) else {}
    cache.market_cache.set(key, data, ttl=float(settings.market_cache_ttl))
    return data


async def fetch_erapi() -> dict[str, Any]:
    cached = cache.market_cache.get("erapi")
    if cached is not None:
        return cached
    data = await _get_json(ERAPI_URL)
    data = data if isinstance(data, dict) else {}
    cache.market_cache.set("erapi", data, ttl=max(600.0, float(settings.market_cache_ttl)))
    return data


async def _nobitex(symbol: str) -> float | None:
    data = await _get_json(f"{settings.nobitex_api_url}/v2/orderbook/{symbol.upper()}IRT")
    if not isinstance(data, dict):
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    best_bid = _to_float(bids[0][0]) if bids else None
    best_ask = _to_float(asks[0][0]) if asks else None
    if best_bid and best_ask:
        return (best_bid + best_ask) / 2
    return best_bid or best_ask


async def _wallex(symbol: str) -> float | None:
    data = await _get_json(f"{settings.wallex_api_url}/v1/markets")
    if not isinstance(data, dict):
        return None
    result = (data.get("result") or {}).get("symbols") or {}
    for key, value in result.items():
        if key.upper().startswith(symbol.upper()):
            price = _to_float(value.get("stats", {}).get("lastPrice")) or _to_float(value.get("lastPrice"))
            if price:
                return price
    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
async def usd_toman_rate() -> float | None:
    """USD price in Toman (used to convert global quotes)."""
    data = await fetch_tgju()
    item = data.get("price_dollar_rl") or {}
    rials = _to_float(item.get("p"))
    if rials:
        return rials / 10.0
    erapi = await fetch_erapi()
    irr = (erapi.get("rates") or {}).get("IRR")
    if irr:
        return float(irr) / 10.0
    return None


async def get_currency(key: str) -> Quote | None:
    if key not in CURRENCIES:
        return None
    _label, tgju_key = CURRENCIES[key]
    label = currency_display_name(key)
    data = await fetch_tgju()
    item = data.get(tgju_key) or {}
    rials = _to_float(item.get("p"))
    if rials:
        return Quote(key=key, label=label, price=rials / 10.0, unit="toman",
                     change=_to_float(item.get("dp")) or 0.0,
                     high=(_to_float(item.get("h")) or 0) / 10.0 or None,
                     low=(_to_float(item.get("l")) or 0) / 10.0 or None,
                     source="tgju", updated=item.get("ts") or "")
    erapi = await fetch_erapi()
    rate = _to_float((erapi.get("rates") or {}).get(key.upper()))
    if rate and rate > 0:
        usd = await usd_toman_rate()
        # ER-API rates are quoted as target-currency units per one USD; invert
        # to display the price of one target-currency unit in Toman or USD.
        price_usd = 1.0 / rate
        price = price_usd * usd if usd else price_usd
        return Quote(key=key, label=label, price=price,
                     unit="toman" if usd else "usd", source="er-api",
                     updated=erapi.get("time_last_update_utc") or "")
    return None


async def get_crypto(key: str) -> Quote | None:
    if key not in CRYPTO:
        return None
    label, tgju_key, coingecko_id = CRYPTO[key]
    data = await fetch_tgju()
    item = data.get(tgju_key) or {}
    usd_price = _to_float(item.get("p"))
    change = _to_float(item.get("dp")) or 0.0
    updated = item.get("ts") or ""
    source = "tgju"

    if usd_price is None:
        coingecko = await fetch_coingecko([coingecko_id])
        entry = (coingecko.get(coingecko_id) or {})
        usd_price = _to_float(entry.get("usd"))
        change = _to_float(entry.get("usd_24h_change")) or 0.0
        source = "coingecko"
        updated = persian_datetime()

    if usd_price is None:
        # Iranian exchanges as a last resort (price in Toman).
        for provider in (_nobitex, _wallex):
            try:
                value = await provider(key)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return Quote(key=key, label=label, price=value / 10.0, unit="toman",
                             change=change, source=provider.__name__.strip("_"), updated=updated)
        return None

    usd_toman = await usd_toman_rate()
    toman_price = usd_price * usd_toman if usd_toman else None
    irr_item = data.get(f"{tgju_key}-irr") or {}
    irr_price = _to_float(irr_item.get("p"))
    if irr_price:
        toman_price = irr_price / 10.0
        source = "tgju"
        updated = irr_item.get("ts") or updated
    if toman_price is None:
        return Quote(key=key, label=label, price=usd_price, unit="usd", change=change,
                     source=source, updated=updated)
    return Quote(key=key, label=label, price=toman_price, unit="toman", change=change,
                 source=source, updated=updated,
                 high=(_to_float(irr_item.get("h")) or 0) / 10.0 or None,
                 low=(_to_float(irr_item.get("l")) or 0) / 10.0 or None)


async def get_gold(key: str) -> Quote | None:
    if key not in GOLD:
        return None
    label, tgju_key, unit = GOLD[key]
    data = await fetch_tgju()
    item = data.get(tgju_key) or {}
    price = _to_float(item.get("p"))
    if price is None:
        return None
    if unit == "toman":
        price = price / 10.0
    return Quote(key=key, label=label, price=price, unit=unit,
                 change=_to_float(item.get("dp")) or 0.0, source="tgju",
                 updated=item.get("ts") or "",
                 high=(_to_float(item.get("h")) or 0) / (10.0 if unit == "toman" else 1) or None,
                 low=(_to_float(item.get("l")) or 0) / (10.0 if unit == "toman" else 1) or None)


async def get_bourse() -> Quote | None:
    data = await fetch_tgju()
    item = data.get("bourse") or {}
    value = _to_float(item.get("p"))
    if value is None:
        return None
    return Quote(key="bourse", label="شاخص کل بورس", price=value, unit="point",
                 change=_to_float(item.get("dp")) or 0.0, source="tgju",
                 updated=item.get("ts") or "")


def provider_note(quote: Quote) -> str:
    names = {
        "tgju": "پایگاه طلا، سکه و ارز (TGJU)",
        "coingecko": "CoinGecko",
        "er-api": "ExchangeRate-API",
        "nobitex": "نوبیتکس",
        "wallex": "والکس",
    }
    return names.get(quote.source, quote.source)


def format_quote_timestamp(value: Any) -> str:
    """Format parseable provider timestamps in the bot's local timezone."""
    if value is None or value == "":
        return ""
    raw = str(value).strip()
    moment: datetime | None = None
    try:
        numeric = float(raw)
        if numeric >= 1_000_000_000:
            if numeric >= 1_000_000_000_000:
                numeric /= 1000
            moment = datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if moment is None:
        iso_text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            candidate = datetime.fromisoformat(iso_text)
            if candidate.tzinfo is not None:
                moment = candidate
        except ValueError:
            try:
                candidate = parsedate_to_datetime(raw)
                if candidate.tzinfo is not None:
                    moment = candidate
            except (TypeError, ValueError, OverflowError):
                pass
    if moment is None:
        return to_persian_digits(raw)
    return persian_datetime(moment, with_seconds=True)


def quote_text(quote: Quote, *, title: str = "") -> str:
    lines = [title or f"💱 قیمت {quote.label}", ""]
    lines.append(f"💰 قیمت: {quote.price_text}")
    if quote.high:
        lines.append(f"📈 بیشترین: {to_persian_digits(f'{quote.high:,.0f}')}")
    if quote.low:
        lines.append(f"📉 کمترین: {to_persian_digits(f'{quote.low:,.0f}')}")
    lines.append(f"📊 تغییر: {quote.change_text}")
    if quote.high is not None or quote.low is not None:
        lines.append("📍 دامنهٔ قیمت: بیشترین/کمترینِ ثبت‌شده در منبع")
    if quote.source:
        lines.append(f"📡 منبع: {provider_note(quote)}")
    if quote.updated:
        stamp = format_quote_timestamp(quote.updated)
        lines.append(f"🕒 آخرین به‌روزرسانی منبع: {stamp}")
    return "\n".join(lines)


async def overview_text(limit_crypto: int = 5) -> str:
    """Compact overview of the Iranian market."""
    lines = ["💱 <b>نرخ لحظه‌ای بازار</b>", ""]
    data = await fetch_tgju()
    if not data:
        lines.append("⚠️ در حال حاضر امکان دریافت اطلاعات وجود ندارد. کمی بعد دوباره تلاش کنید.")
        return "\n".join(lines)

    lines.append("<b>💱 ارزهای منتخب</b>")
    for key in ("usd", "eur", "gbp", "try", "aed"):
        quote = await get_currency(key)
        if quote:
            lines.append(f"• {quote.label}: {quote.price_text}  {quote.change_text}")

    lines.extend(["", "<b>🥇 طلا و سکه</b>"])
    for key in ("coin_emami", "gram18"):
        quote = await get_gold(key)
        if quote:
            lines.append(f"• {quote.label}: {quote.price_text}  {quote.change_text}")

    lines.extend(["", "<b>🪙 ارزهای دیجیتال</b>"])
    for key in list(CRYPTO)[:limit_crypto]:
        quote = await get_crypto(key)
        if quote:
            lines.append(f"• {quote.label}: {quote.price_text}  {quote.change_text}")

    bourse = await get_bourse()
    if bourse:
        lines.extend(["", "<b>📈 بورس</b>",
                      f"• {bourse.label}: {bourse.price_text}  {bourse.change_text}"])

    lines.extend(["", f"🕒 به‌روزرسانی صفحه: {persian_datetime()}"])
    return "\n".join(lines)


def crypto_list_text() -> str:
    names = ", ".join(label for label, _, _ in CRYPTO.values())
    return (f"🪙 ارزهای پشتیبانی‌شده:\n{names}\n\n"
            "مثال: <code>قیمت بیت‌کوین</code> یا <code>ارز btc</code>")


def currency_list_text() -> str:
    lines = ["💱 <b>ارزهای پشتیبانی‌شده</b>", ""]
    listed: set[str] = set()
    for category, keys in CURRENCY_CATEGORIES:
        lines.append(f"<b>{category}</b>")
        for key in keys:
            if key not in CURRENCIES:
                continue
            listed.add(key)
            lines.append(f"• {currency_display_name(key)} — <code>{key.upper()}</code>")
        lines.append("")
    # Keep a newly added currency visible even before its category is curated.
    ungrouped = [key for key in CURRENCIES if key not in listed]
    if ungrouped:
        lines.append("<b>💱 سایر ارزها</b>")
        lines.extend(f"• {currency_display_name(key)} — <code>{key.upper()}</code>"
                     for key in ungrouped)
    lines.extend([
        "اولویت با نرخ بازار ایران و نمایش به تومان است؛ اگر دادهٔ داخلی در دسترس نباشد، "
        "نرخ مرجع جایگزین نمایش داده می‌شود و واحد آن کنار قیمت مشخص است.",
        "نمونه: <code>قیمت دلار</code> • <code>قیمت یورو</code> • <code>قیمت لیر</code>",
    ])
    return "\n".join(lines)
