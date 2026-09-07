"""FakeTransport — площадка в памяти, говорящая структурами ccxt. Для тестов и офлайн-режима.

Умеет: свечи/сделки/стакан/тикер/фандинг; ордера с clientOrderId (идемпотентно), филлы,
позиции, баланс, плечо; права ключа; сбои: `offline` (NetworkError на всё),
`timeout_after_accept` (ордер принят, но ответ потерян — RequestTimeout),
`fill_open_orders()` — «площадка исполнила, пока связи не было».
Исключения — настоящие классы ccxt, чтобы код обработки был общим с живым транспортом.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import ccxt

from lab.contracts.timeframes import parse_tf
from lab.feeds.cex.transport import VENUE_SPECS

_ms = 1000


def to_ms(ts: datetime) -> int:
    return int(ts.timestamp() * _ms)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / _ms, tz=UTC)


class FakeTransport:
    def __init__(
        self,
        venue: str = "bybit",
        *,
        has_keys: bool = True,
        rights: dict[str, bool] | None = None,
        taker_bps: Decimal = Decimal("10"),
        max_leverage: int | None = None,
        balance: dict[str, Decimal] | None = None,
        default_mid: Decimal | None = None,
    ) -> None:
        self.default_mid = default_mid
        spec = VENUE_SPECS[venue]
        self.id = venue
        self.has_keys = has_keys
        self.rights = rights or {"trade": has_keys, "withdraw": False}
        self.taker_bps = taker_bps
        self.max_leverage = max_leverage or 50
        self.quote = spec.quote_asset
        self.balance = dict(balance or {self.quote: Decimal("10000")})
        self.ohlcv: dict[tuple[str, str], list[list[Any]]] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.trades: dict[str, list[dict]] = {}
        self.books: dict[str, dict[str, Any]] = {}
        self.funding: dict[str, dict[str, Any]] = {}
        self.funding_history: list[dict] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.my_trades: list[dict] = []
        self.positions: dict[str, dict[str, Any]] = {}
        self.leverage: dict[str, int] = {}
        self.calls: list[str] = []
        self.offline = False
        self.timeout_after_accept = False
        self.fail_next: Exception | None = None
        self.ohlcv_fail_after: int | None = None  # обрыв после N успешных fetch_ohlcv
        self.now = datetime(2026, 6, 1, tzinfo=UTC)
        self._ids = count(1)

    # -- подготовка сцены ---------------------------------------------------------

    def seed_ohlcv(
        self, symbol: str, tf: str, start: datetime, n: int, *, start_price: Decimal
    ) -> None:
        step = parse_tf(tf)
        rows = []
        price = start_price
        for i in range(n):
            o = price
            c = price + Decimal(1) * (1 if i % 2 == 0 else -1)
            rows.append(
                [
                    to_ms(start + step * i),
                    float(o),
                    float(max(o, c) + 1),
                    float(min(o, c) - 1),
                    float(c),
                    float(10 + i),
                ]
            )
            price = c
        self.ohlcv[(symbol, tf)] = rows
        self.set_ticker(symbol, price)

    def set_ticker(self, symbol: str, mid: Decimal, spread: Decimal = Decimal("1")) -> None:
        self.tickers[symbol] = {
            "symbol": symbol,
            "bid": float(mid - spread / 2),
            "ask": float(mid + spread / 2),
            "last": float(mid),
            "timestamp": to_ms(self.now),
        }
        self.books[symbol] = {
            "symbol": symbol,
            "timestamp": to_ms(self.now),
            "bids": [[float(mid - spread / 2 - i), 10.0] for i in range(5)],
            "asks": [[float(mid + spread / 2 + i), 10.0] for i in range(5)],
        }

    def set_funding(
        self,
        symbol: str,
        rate: Decimal,
        next_at: datetime | None = None,
        mark: Decimal | None = None,
    ) -> None:
        self.funding[symbol] = {
            "symbol": symbol,
            "fundingRate": float(rate),
            "fundingTimestamp": to_ms(next_at or self.now + timedelta(hours=8)),
            "markPrice": float(mark) if mark is not None else self.tickers[symbol]["last"],
        }

    def add_funding_payment(self, symbol: str, amount: Decimal, ts: datetime) -> None:
        self.funding_history.append(
            {
                "id": f"fund-{next(self._ids)}",
                "symbol": symbol,
                "amount": float(amount),
                "timestamp": to_ms(ts),
                "code": self.quote,
            }
        )

    def fill_open_orders(self) -> None:
        """Площадка исполнила все открытые ордера (например, пока связи не было)."""
        for order in list(self.orders.values()):
            if order["status"] == "open":
                price = order["price"]
                self._fill(
                    order,
                    Decimal(str(price))
                    if price is not None
                    else self._exec_price(order["symbol"], order["side"]),
                )

    # -- сбои -------------------------------------------------------------------------

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.offline:
            raise ccxt.NetworkError(f"{self.id}: connection lost")
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err

    # -- Transport ------------------------------------------------------------------

    def load_markets(self) -> dict[str, Any]:
        self._guard("load_markets")
        return {}

    def market(self, symbol: str) -> dict[str, Any]:
        swap = ":" in symbol
        return {
            "symbol": symbol,
            "swap": swap,
            "spot": not swap,
            "quote": symbol.split("/")[1].split(":")[0],
            "limits": {"leverage": {"max": self.max_leverage if swap else 1}},
        }

    def fetch_time(self) -> int:
        self._guard("fetch_time")
        return to_ms(self.now)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None) -> list[list[Any]]:
        self._guard("fetch_ohlcv")
        if self.ohlcv_fail_after is not None:
            if self.ohlcv_fail_after <= 0:
                self.ohlcv_fail_after = None
                raise ccxt.NetworkError(f"{self.id}: connection lost")
            self.ohlcv_fail_after -= 1
        rows = self.ohlcv.get((symbol, timeframe), [])
        if since is not None:
            rows = [r for r in rows if r[0] >= since]
        return rows[: limit or len(rows)]

    def fetch_trades(self, symbol, since=None, limit=None) -> list[dict]:
        self._guard("fetch_trades")
        rows = self.trades.get(symbol, [])
        if since is not None:
            rows = [r for r in rows if r["timestamp"] >= since]
        return rows[: limit or len(rows)]

    def _ensure_ticker(self, symbol: str) -> None:
        if symbol not in self.tickers and self.default_mid is not None:
            self.set_ticker(symbol, self.default_mid, spread=Decimal(0))

    def fetch_order_book(self, symbol, limit=None) -> dict[str, Any]:
        self._guard("fetch_order_book")
        self._ensure_ticker(symbol)
        return self.books[symbol]

    def fetch_ticker(self, symbol) -> dict[str, Any]:
        self._guard("fetch_ticker")
        self._ensure_ticker(symbol)
        return self.tickers[symbol]

    def fetch_funding_rate(self, symbol) -> dict[str, Any]:
        self._guard("fetch_funding_rate")
        return self.funding[symbol]

    def fetch_funding_history(self, symbol=None, since=None) -> list[dict]:
        self._guard("fetch_funding_history")
        return [
            r
            for r in self.funding_history
            if (symbol is None or r["symbol"] == symbol)
            and (since is None or r["timestamp"] >= since)
        ]

    def create_order(self, symbol, type, side, amount, price=None, params=None) -> dict[str, Any]:
        self._guard("create_order")
        if not self.has_keys:
            raise ccxt.AuthenticationError(f"{self.id}: apiKey required")
        params = params or {}
        coid = params.get("clientOrderId")
        if coid and any(o["clientOrderId"] == coid for o in self.orders.values()):
            raise ccxt.InvalidOrder(f"{self.id}: duplicate clientOrderId {coid}")
        order = {
            "id": f"{self.id}-{next(self._ids)}",
            "clientOrderId": coid,
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": float(amount),
            "price": float(price) if price is not None else None,
            "filled": 0.0,
            "average": None,
            "status": "open",
            "timestamp": to_ms(self.now),
            "fee": None,
            "reduceOnly": bool(params.get("reduceOnly", False)),
        }
        self.orders[order["id"]] = order
        exec_price = self._exec_price(symbol, side)
        if type == "market":
            self._fill(order, exec_price)
        elif price is not None and (
            (side == "buy" and Decimal(str(price)) >= exec_price)
            or (side == "sell" and Decimal(str(price)) <= exec_price)
        ):
            self._fill(order, Decimal(str(price)))
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise ccxt.RequestTimeout(f"{self.id}: request timed out")
        return dict(order)

    def cancel_order(self, id, symbol=None) -> dict[str, Any]:
        self._guard("cancel_order")
        order = self.orders[id]
        if order["status"] == "open":
            order["status"] = "canceled"
        return dict(order)

    def fetch_order(self, id, symbol=None) -> dict[str, Any]:
        self._guard("fetch_order")
        if id not in self.orders:
            raise ccxt.OrderNotFound(f"{self.id}: order {id} not found")
        return dict(self.orders[id])

    def fetch_open_orders(self, symbol=None) -> list[dict]:
        self._guard("fetch_open_orders")
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] == "open" and (symbol is None or o["symbol"] == symbol)
        ]

    def fetch_closed_orders(self, symbol=None, since=None) -> list[dict]:
        self._guard("fetch_closed_orders")
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] != "open"
            and (symbol is None or o["symbol"] == symbol)
            and (since is None or o["timestamp"] >= since)
        ]

    def fetch_my_trades(self, symbol=None, since=None) -> list[dict]:
        self._guard("fetch_my_trades")
        return [
            dict(t)
            for t in self.my_trades
            if (symbol is None or t["symbol"] == symbol)
            and (since is None or t["timestamp"] >= since)
        ]

    def fetch_positions(self, symbols=None) -> list[dict]:
        self._guard("fetch_positions")
        return [dict(p) for s, p in self.positions.items() if symbols is None or s in symbols]

    def fetch_balance(self) -> dict[str, Any]:
        self._guard("fetch_balance")
        return {
            "total": {k: float(v) for k, v in self.balance.items()},
            "free": {k: float(v) for k, v in self.balance.items()},
            "timestamp": to_ms(self.now),
        }

    def set_leverage(self, leverage, symbol) -> Any:
        self._guard("set_leverage")
        if leverage > self.max_leverage:
            raise ccxt.BadRequest(f"{self.id}: leverage {leverage} > max {self.max_leverage}")
        self.leverage[symbol] = int(leverage)
        return {"leverage": leverage}

    def key_rights(self) -> dict[str, bool]:
        self._guard("key_rights")
        return dict(self.rights)

    # -- внутреннее -------------------------------------------------------------------

    def _exec_price(self, symbol: str, side: str) -> Decimal:
        self._ensure_ticker(symbol)
        t = self.tickers[symbol]
        return Decimal(str(t["ask"] if side == "buy" else t["bid"]))

    def _fill(self, order: dict[str, Any], price: Decimal) -> None:
        qty = Decimal(str(order["amount"]))
        fee = price * qty * self.taker_bps / Decimal(10_000)
        order.update(
            status="closed",
            filled=float(qty),
            average=float(price),
            fee={"cost": float(fee), "currency": self.quote},
        )
        self.my_trades.append(
            {
                "id": f"t-{next(self._ids)}",
                "order": order["id"],
                "symbol": order["symbol"],
                "side": order["side"],
                "price": float(price),
                "amount": float(qty),
                "fee": {"cost": float(fee), "currency": self.quote},
                "timestamp": to_ms(self.now),
            }
        )
        signed = qty if order["side"] == "buy" else -qty
        self.balance[self.quote] = self.balance[self.quote] - signed * price - fee
        pos = self.positions.get(order["symbol"])
        net = Decimal(0)
        if pos:
            net = Decimal(str(pos["contracts"])) * (1 if pos["side"] == "long" else -1)
        net += signed
        if net == 0:
            self.positions.pop(order["symbol"], None)
            return
        lev = self.leverage.get(order["symbol"], 1)
        liq = price * (1 - Decimal(1) / lev) if net > 0 else price * (1 + Decimal(1) / lev)
        self.positions[order["symbol"]] = {
            "symbol": order["symbol"],
            "side": "long" if net > 0 else "short",
            "contracts": float(abs(net)),
            "entryPrice": float(price),
            "leverage": lev,
            "unrealizedPnl": 0.0,
            "liquidationPrice": float(liq) if ":" in order["symbol"] else None,
            "timestamp": to_ms(self.now),
        }


__all__ = ["FakeTransport", "from_ms", "to_ms"]
