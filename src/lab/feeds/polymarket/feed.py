"""Фид Polymarket: Gamma (рынки), CLOB (история цен, стакан), Data API (лидерборд, позиции).

Чтение — без ключа и без KYC (research-sources §7), поэтому фид живёт отдельно от исполнителя:
даже когда торговля с нашего IP запрещена, ветка `prediction` продолжает мерить.

Инструмент здесь — `token_id` исхода (ERC-1155 позиция «Yes»/«No»), а не рынок: цена и стакан
существуют у исхода. Цена исхода — это вероятность, поэтому свеча вырождена (OHLC равны):
у `/prices-history` одна точка = одна цена, придумывать max/min из неё было бы враньём.

Лимиты Data API (1000 req / 10 с, `/positions` 150, `/trades` 200) сведены к весам квоты:
вес = 1000 / лимит эндпоинта, так что дорогие эндпоинты быстрее выбирают бюджет `feeds_registry`.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lab.contracts import Book, BookLevel, Candle, Event, Health, Trade
from lab.contracts.timeframes import parse_tf
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.chains.transport import ChainError, HttpTransport, HttpxTransport

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"

FEED_ID = "polymarket"
DATA_FEED_ID = "polymarket_data"

#: лимит запросов на 10 с по документации; вес квоты = DATA_WINDOW_LIMIT / лимит эндпоинта
DATA_WINDOW_LIMIT = 1000
DATA_ENDPOINT_LIMITS = {"/positions": 150, "/trades": 200, "/activity": 200}

POSITION_EVENT = "pm_position"


class PolymarketError(RuntimeError):
    """Отказ Polymarket API: нет связи, гео-блок, неразобранный ответ."""


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as err:
        raise PolymarketError(f"Polymarket: не число {value!r}") from err


def _json_list(value: Any) -> list[Any]:
    """Gamma отдаёт списки строкой JSON (`'["Yes", "No"]'`) — иногда уже списком."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ts(value: Any) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class MarketToken:
    token_id: str
    outcome: str
    price: Decimal


@dataclass(frozen=True)
class Market:
    condition_id: str
    question: str
    slug: str
    tokens: list[MarketToken]
    end_date: datetime | None
    closed: bool
    active: bool
    volume: Decimal
    liquidity: Decimal

    def token(self, outcome: str) -> MarketToken | None:
        for t in self.tokens:
            if t.outcome.lower() == outcome.lower():
                return t
        return None


@dataclass(frozen=True)
class LeaderEntry:
    address: str
    name: str
    pnl_usd: Decimal
    volume_usd: Decimal
    rank: int


@dataclass(frozen=True)
class PmPosition:
    wallet: str
    condition_id: str
    token_id: str
    outcome: str
    size: Decimal
    avg_price: Decimal
    current_price: Decimal
    value_usd: Decimal
    title: str
    slug: str
    redeemable: bool
    ts: datetime

    @property
    def cost_usd(self) -> Decimal:
        return self.size * self.avg_price


def pm_position_payload(position: PmPosition) -> dict[str, Any]:
    """Событие позиции лидера — вход стратегии `pm-copy-*` (ключи стабильны)."""
    return {
        "wallet": position.wallet,
        "condition_id": position.condition_id,
        "token_id": position.token_id,
        "outcome": position.outcome,
        "size": str(position.size),
        "avg_price": str(position.avg_price),
        "price": str(position.current_price),
        "value_usd": str(position.value_usd),
        "title": position.title,
        "slug": position.slug,
        "redeemable": position.redeemable,
        "ts": position.ts.isoformat(),
        "venue": "polymarket",
    }


class PolymarketFeed:
    """Контракт `Feed` целиком. Инструмент — `token_id` исхода."""

    id = FEED_ID
    venue = "polymarket"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        quota: QuotaSink | None = None,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        data_base: str = DATA_BASE,
    ) -> None:
        self.transport = transport or HttpxTransport()
        self.quota = quota or NullQuota()
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.data_base = data_base.rstrip("/")

    # -- сеть под квотой -----------------------------------------------------------

    def _get(
        self, url: str, params: dict | None = None, *, feed_id: str = FEED_ID, weight: int = 1
    ) -> Any:
        self.quota.use(feed_id, weight)
        try:
            return self.transport.get(url, params=params)
        except ChainError as err:
            raise PolymarketError(f"polymarket: {err}") from err

    def _data_get(self, path: str, params: dict | None = None) -> Any:
        limit = DATA_ENDPOINT_LIMITS.get(path, DATA_WINDOW_LIMIT)
        weight = max(1, math.ceil(DATA_WINDOW_LIMIT / limit))
        return self._get(f"{self.data_base}{path}", params, feed_id=DATA_FEED_ID, weight=weight)

    # -- Gamma: рынки ---------------------------------------------------------------

    def markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool | None = False,
        slug: str | None = None,
    ) -> list[Market]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if slug:
            params["slug"] = slug
        raw = self._get(f"{self.gamma_base}/markets", params)
        rows = raw.get("data", []) if isinstance(raw, dict) else raw
        return [self._market(row) for row in rows or []]

    def market(self, condition_id: str) -> Market | None:
        raw = self._get(f"{self.gamma_base}/markets", {"condition_ids": condition_id, "limit": 1})
        rows = raw.get("data", []) if isinstance(raw, dict) else raw
        return self._market(rows[0]) if rows else None

    def _market(self, row: dict[str, Any]) -> Market:
        ids = [str(t) for t in _json_list(row.get("clobTokenIds"))]
        outcomes = [str(o) for o in _json_list(row.get("outcomes"))] or ["Yes", "No"]
        prices = [_dec(p) for p in _json_list(row.get("outcomePrices"))]
        tokens = [
            MarketToken(
                token_id=token_id,
                outcome=outcomes[i] if i < len(outcomes) else str(i),
                price=prices[i] if i < len(prices) else Decimal(0),
            )
            for i, token_id in enumerate(ids)
        ]
        end = row.get("endDate") or row.get("end_date_iso")
        return Market(
            condition_id=str(row.get("conditionId") or row.get("condition_id") or ""),
            question=str(row.get("question", "")),
            slug=str(row.get("slug", "")),
            tokens=tokens,
            end_date=_ts(end) if end else None,
            closed=bool(row.get("closed", False)),
            active=bool(row.get("active", True)),
            volume=_dec(row.get("volume")),
            liquidity=_dec(row.get("liquidity")),
        )

    # -- CLOB: история цен и стакан --------------------------------------------------

    def prices_history(
        self,
        token_id: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        fidelity_min: int = 60,
    ) -> list[tuple[datetime, Decimal]]:
        params: dict[str, Any] = {"market": token_id, "fidelity": fidelity_min}
        if from_ts is not None:
            params["startTs"] = int(from_ts.timestamp())
        if to_ts is not None:
            params["endTs"] = int(to_ts.timestamp())
        raw = self._get(f"{self.clob_base}/prices-history", params)
        points = raw.get("history", []) if isinstance(raw, dict) else raw or []
        return [(_ts(p.get("t")), _dec(p.get("p"))) for p in points]

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]:
        fidelity = max(1, int(parse_tf(tf).total_seconds() // 60))
        points = self.prices_history(instrument, from_ts, to_ts, fidelity_min=fidelity)
        return [
            Candle(
                instrument=instrument,
                tf=tf,
                ts=ts,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(0),
            )
            for ts, price in points
        ]

    def book(self, instrument: str, depth: int = 50) -> Book:
        raw = self._get(f"{self.clob_base}/book", {"token_id": instrument})
        if not isinstance(raw, dict):
            raise PolymarketError(f"polymarket: непонятный стакан по {instrument}")
        bids = [
            BookLevel(price=_dec(x.get("price")), qty=_dec(x.get("size")))
            for x in raw.get("bids", [])
        ]
        asks = [
            BookLevel(price=_dec(x.get("price")), qty=_dec(x.get("size")))
            for x in raw.get("asks", [])
        ]
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        return Book(
            instrument=instrument,
            ts=datetime.now(UTC),
            bids=bids[:depth],
            asks=asks[:depth],
        )

    def price(self, instrument: str, side: str = "buy") -> Decimal | None:
        raw = self._get(f"{self.clob_base}/price", {"token_id": instrument, "side": side})
        if isinstance(raw, dict) and raw.get("price") is not None:
            return _dec(raw["price"])
        return None

    # -- Data API: сделки, лидерборд, позиции ----------------------------------------

    def trades(
        self, instrument: str, from_ts: datetime | None = None, to_ts: datetime | None = None
    ) -> Sequence[Trade]:
        """`instrument` — condition_id рынка; сделки рынка из Data API."""
        params: dict[str, Any] = {"market": instrument, "limit": 500}
        rows = self._data_get("/trades", params) or []
        out: list[Trade] = []
        for row in rows:
            ts = _ts(row.get("timestamp") or row.get("ts"))
            if from_ts and ts < from_ts:
                continue
            if to_ts and ts > to_ts:
                continue
            side = str(row.get("side", "buy")).lower()
            out.append(
                Trade(
                    instrument=str(row.get("asset") or row.get("token_id") or instrument),
                    ts=ts,
                    price=_dec(row.get("price")),
                    qty=_dec(row.get("size")),
                    side="buy" if side.startswith("b") else "sell",
                )
            )
        return out

    def wallet_trades(self, wallet: str, *, limit: int = 500) -> list[dict[str, Any]]:
        return list(self._data_get("/trades", {"user": wallet, "limit": limit}) or [])

    def leaderboard(
        self, *, window: str = "all", limit: int = 20, order_by: str = "pnl"
    ) -> list[LeaderEntry]:
        raw = self._data_get(
            "/v1/leaderboard", {"window": window, "limit": limit, "orderBy": order_by}
        )
        rows = raw.get("data", raw) if isinstance(raw, dict) else raw or []
        out: list[LeaderEntry] = []
        for i, row in enumerate(rows, start=1):
            out.append(
                LeaderEntry(
                    address=str(
                        row.get("proxyWallet") or row.get("wallet") or row.get("address") or ""
                    ),
                    name=str(row.get("name") or row.get("pseudonym") or ""),
                    pnl_usd=_dec(row.get("amount") if "amount" in row else row.get("pnl")),
                    volume_usd=_dec(row.get("volume")),
                    rank=int(row.get("rank") or i),
                )
            )
        return out

    def positions(self, wallet: str, *, limit: int = 100, closed: bool = False) -> list[PmPosition]:
        path = "/closed-positions" if closed else "/positions"
        rows = self._data_get(path, {"user": wallet, "limit": limit}) or []
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        now = datetime.now(UTC)
        return [
            PmPosition(
                wallet=str(row.get("proxyWallet") or wallet),
                condition_id=str(row.get("conditionId") or ""),
                token_id=str(row.get("asset") or row.get("tokenId") or ""),
                outcome=str(row.get("outcome") or ""),
                size=_dec(row.get("size")),
                avg_price=_dec(row.get("avgPrice")),
                current_price=_dec(row.get("curPrice")),
                value_usd=_dec(row.get("currentValue")),
                title=str(row.get("title") or ""),
                slug=str(row.get("slug") or ""),
                redeemable=bool(row.get("redeemable", False)),
                ts=now,
            )
            for row in rows
        ]

    # -- события ---------------------------------------------------------------------

    async def events(
        self, kind: str = POSITION_EVENT, *, wallets: Sequence[str] = (), poll_s: float = 30.0
    ) -> AsyncIterator[Event]:
        """Позиции отслеживаемых кошельков опросом: WS-канала у Data API нет."""
        import asyncio

        if kind != POSITION_EVENT:
            raise ValueError(f"polymarket: неизвестный вид событий {kind!r}")
        seen: dict[str, str] = {}
        while True:
            for wallet in wallets:
                for position in self.positions(wallet):
                    key = f"{wallet}:{position.token_id}"
                    size = str(position.size)
                    if seen.get(key) == size:
                        continue
                    seen[key] = size
                    yield Event(kind=kind, ts=position.ts, payload=pm_position_payload(position))
            await asyncio.sleep(poll_s)

    # -- здоровье ---------------------------------------------------------------------

    def health(self) -> Health:
        now = datetime.now(UTC)
        try:
            self._get(f"{self.clob_base}/ok")
        except PolymarketError as err:
            return Health(status="down", detail=f"polymarket: {err}", checked_at=now)
        return Health(status="ok", detail="polymarket: чтение без ключа", checked_at=now)


def make_polymarket_feed(
    transport: HttpTransport | None = None, *, quota: QuotaSink | None = None
) -> PolymarketFeed:
    return PolymarketFeed(transport, quota=quota)


__all__ = [
    "CLOB_BASE",
    "DATA_BASE",
    "DATA_ENDPOINT_LIMITS",
    "DATA_FEED_ID",
    "FEED_ID",
    "GAMMA_BASE",
    "POSITION_EVENT",
    "LeaderEntry",
    "Market",
    "MarketToken",
    "PmPosition",
    "PolymarketError",
    "PolymarketFeed",
    "make_polymarket_feed",
    "pm_position_payload",
]
