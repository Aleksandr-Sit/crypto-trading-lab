"""Robinhood Crypto API: подпись Ed25519, котировки и счёт за контрактом `Feed` (История 85).

Официальный API (с 05.2024) доступен только клиентам Robinhood Crypto из США с пройденным KYC
(`research-sources.md` §8), поэтому отказ 403 здесь — не поломка, а ожидаемый исход: ветка `rh`
переходит в режим сигналов (`executors.access`), а не выключается.

Свечей у API нет — `candles` поднимает `RobinhoodUnsupported`: молчаливый пустой список
выглядел бы как «данных за период нет». Дневные свечи акций живут в `feeds.stocks` (таск 07).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from lab.contracts import Book, BookLevel, Candle, Event, Health, Trade
from lab.feeds import NullQuota, QuotaSink

BASE_URL = "https://trading.robinhood.com"
FEED_ID = "robinhood"
KEY_ENV = "ROBINHOOD_API_KEY"
SECRET_ENV = "ROBINHOOD_PRIVATE_KEY"

QUOTE_PATH = "/api/v1/crypto/marketdata/best_bid_ask/"
ACCOUNT_PATH = "/api/v1/crypto/trading/accounts/"
HOLDINGS_PATH = "/api/v1/crypto/trading/holdings/"
ORDERS_PATH = "/api/v1/crypto/trading/orders/"
PAIRS_PATH = "/api/v1/crypto/trading/trading_pairs/"

GEO_MARKERS = ("403", "401", "region", "not available", "unauthorized")


class RobinhoodError(RuntimeError):
    """Отказ Robinhood API: нет связи, нет прав, неразобранный ответ."""


class RobinhoodUnsupported(NotImplementedError):
    """Метод контракта, которого у Crypto API нет (свечи, лента сделок рынка)."""


class RobinhoodUnavailable(RobinhoodError):
    """API закрыт для этого аккаунта/региона — ветка `rh` живёт в режиме сигналов (История 86)."""


@runtime_checkable
class RhTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        body: str | None = None,
        headers: dict | None = None,
    ) -> Any: ...


class Ed25519Signer:
    """Подпись запроса: `api_key + timestamp + path + method + body`, Ed25519, base64.

    Приватный ключ — base64 (seed 32 байта или полный ключ 64), как его отдаёт Robinhood.
    """

    def __init__(self, api_key: str, private_key: str) -> None:
        self.api_key = api_key
        self._private_key = private_key

    def _key(self) -> Any:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        try:
            raw = base64.b64decode(self._private_key)
        except Exception as err:  # noqa: BLE001 — формат ключа задаёт пользователь
            raise RobinhoodError(f"{SECRET_ENV}: ключ не в base64") from err
        if len(raw) not in (32, 64):
            raise RobinhoodError(f"{SECRET_ENV}: ожидались 32 или 64 байта, пришло {len(raw)}")
        return Ed25519PrivateKey.from_private_bytes(raw[:32])

    def headers(
        self, method: str, path: str, *, body: str = "", ts: int | None = None
    ) -> dict[str, str]:
        stamp = int(ts if ts is not None else time.time())
        message = f"{self.api_key}{stamp}{path}{method.upper()}{body}".encode()
        signature = self._key().sign(message)
        return {
            "x-api-key": self.api_key,
            "x-signature": base64.b64encode(signature).decode(),
            "x-timestamp": str(stamp),
            "Content-Type": "application/json; charset=utf-8",
        }


class HttpxRhTransport:
    """Живой транспорт. `httpx` импортируется лениво — без сети клиент просто не создаётся."""

    def __init__(self, *, timeout_s: float = 15.0, client: Any = None) -> None:
        self.timeout_s = timeout_s
        self._client = client

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        body: str | None = None,
        headers: dict | None = None,
    ) -> Any:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)
        try:
            response = self._client.request(
                method, url, params=params, content=body, headers=headers
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except httpx.HTTPStatusError as err:
            raise RobinhoodError(
                f"{url}: {err.response.status_code} {err.response.text[:200]}"
            ) from err
        except httpx.HTTPError as err:
            raise RobinhoodError(f"{url}: нет связи ({err})") from err


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value not in (None, "") else default))


def _ts(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RobinhoodFeed:
    """Контракт `Feed` для крипты Robinhood. Инструмент — пара вида `BTC-USD`."""

    id = FEED_ID
    venue = "robinhood"

    def __init__(
        self,
        transport: RhTransport | None = None,
        *,
        api_key: str | None = None,
        private_key: str | None = None,
        quota: QuotaSink | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.transport = transport or HttpxRhTransport()
        self.api_key = api_key
        self.quota = quota or NullQuota()
        self.base_url = base_url.rstrip("/")
        self.signer = Ed25519Signer(api_key, private_key) if api_key and private_key else None
        self._signed_calls = 0

    # -- подписанный вызов -------------------------------------------------------------

    def has_keys(self) -> bool:
        return self.signer is not None

    def calls_signed(self) -> int:
        return self._signed_calls

    def call(
        self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None
    ) -> Any:
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers: dict[str, str] = {}
        if self.signer is not None:
            headers = self.signer.headers(method, path, body=body)
            self._signed_calls += 1
        self.quota.use(FEED_ID, 1)
        return self.transport.request(
            method.upper(),
            f"{self.base_url}{path}",
            params=params,
            body=body or None,
            headers=headers,
        )

    # -- данные -------------------------------------------------------------------------

    def quote(self, instrument: str) -> dict[str, Any]:
        raw = self.call("GET", QUOTE_PATH, params={"symbol": instrument})
        results = raw.get("results", []) if isinstance(raw, dict) else []
        if not results:
            raise RobinhoodError(f"robinhood: пустая котировка по {instrument}")
        return results[0]

    def ticker(self, instrument: str) -> dict[str, Decimal]:
        row = self.quote(instrument)
        return {
            "bid": _dec(row.get("bid_inclusive_of_sell_spread")),
            "ask": _dec(row.get("ask_inclusive_of_buy_spread")),
            "last": _dec(row.get("price")),
        }

    def book(self, instrument: str, depth: int = 1) -> Book:
        """Стакана API не отдаёт — только лучшие цены со спредом, из них и строим стакан."""
        row = self.quote(instrument)
        return Book(
            instrument=instrument,
            ts=_ts(row.get("timestamp")),
            bids=[BookLevel(price=_dec(row.get("bid_inclusive_of_sell_spread")), qty=Decimal(0))],
            asks=[BookLevel(price=_dec(row.get("ask_inclusive_of_buy_spread")), qty=Decimal(0))],
        )

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]:
        raise RobinhoodUnsupported(
            "robinhood: свечей в Crypto API нет — история берётся у биржевых фидов, "
            "дневные свечи акций — у feeds.stocks"
        )

    def trades(self, instrument: str, from_ts: datetime, to_ts: datetime) -> Sequence[Trade]:
        raise RobinhoodUnsupported("robinhood: ленты сделок рынка в Crypto API нет")

    async def events(
        self, kind: str = "ticker", *, instruments: Sequence[str] = (), poll_s: float = 30.0
    ) -> AsyncIterator[Event]:
        import asyncio

        if kind != "ticker":
            raise ValueError(f"robinhood: неизвестный вид событий {kind!r}")
        while True:
            for instrument in instruments:
                prices = self.ticker(instrument)
                yield Event(
                    kind=kind,
                    ts=datetime.now(UTC),
                    payload={
                        "instrument": instrument,
                        **{k: str(v) for k, v in prices.items()},
                        "venue": "robinhood",
                    },
                )
            await asyncio.sleep(poll_s)

    # -- счёт ----------------------------------------------------------------------------

    def account(self) -> dict[str, Any]:
        raw = self.call("GET", ACCOUNT_PATH)
        return raw if isinstance(raw, dict) else {}

    def holdings(self, asset_code: str | None = None) -> list[dict[str, Any]]:
        params = {"asset_code": asset_code} if asset_code else None
        raw = self.call("GET", HOLDINGS_PATH, params=params)
        return list(raw.get("results", [])) if isinstance(raw, dict) else []

    def orders(self, params: dict | None = None) -> list[dict[str, Any]]:
        raw = self.call("GET", ORDERS_PATH, params=params)
        if isinstance(raw, dict):
            return list(raw.get("results", []))
        return list(raw or [])

    def trading_pairs(self) -> list[dict[str, Any]]:
        raw = self.call("GET", PAIRS_PATH)
        return list(raw.get("results", [])) if isinstance(raw, dict) else []

    # -- здоровье --------------------------------------------------------------------------

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.signer is None:
            return Health(
                status="down",
                detail=f"нет ключей {KEY_ENV}/{SECRET_ENV}: robinhood недоступен",
                checked_at=now,
            )
        try:
            self.account()
        except RobinhoodError as err:
            text = str(err)
            if any(marker in text.lower() for marker in GEO_MARKERS):
                return Health(
                    status="down",
                    detail=f"robinhood закрыт для аккаунта/региона: {text}",
                    checked_at=now,
                )
            return Health(status="down", detail=f"robinhood: {text}", checked_at=now)
        return Health(status="ok", detail="robinhood: аккаунт доступен", checked_at=now)


def make_robinhood_feed(
    transport: RhTransport | None = None,
    *,
    quota: QuotaSink | None = None,
    env: dict[str, str] | None = None,
) -> RobinhoodFeed:
    """Ключи — только из окружения по именам из `.env.example`."""
    import os

    source = os.environ if env is None else env
    return RobinhoodFeed(
        transport,
        api_key=source.get(KEY_ENV) or None,
        private_key=source.get(SECRET_ENV) or None,
        quota=quota,
    )


__all__ = [
    "ACCOUNT_PATH",
    "BASE_URL",
    "FEED_ID",
    "HOLDINGS_PATH",
    "KEY_ENV",
    "ORDERS_PATH",
    "QUOTE_PATH",
    "SECRET_ENV",
    "Ed25519Signer",
    "HttpxRhTransport",
    "RhTransport",
    "RobinhoodError",
    "RobinhoodFeed",
    "RobinhoodUnavailable",
    "RobinhoodUnsupported",
    "make_robinhood_feed",
]
