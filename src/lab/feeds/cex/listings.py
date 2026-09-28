"""Лента новых листингов Binance: спотовые пары к USDT, запуск перпов, день листинга.

Зачем. Шорт листингов (`strategies.listing`) торгует СОБЫТИЕ — появление монеты на споте
Binance, — и сам нового листинга не увидит: его состав и даты заливались разово, скриптом
по архиву (`scripts/listing_universe.py`), и застыли на июле 2026 года.

Источник проверен ВЫЗОВОМ 28.09.2026 (CLAUDE.md, «Тариф источника»): три публичных
эндпоинта, без ключа и без регистрации.

* `fapi/v1/exchangeInfo` (вес 1) — 658 бессрочных контрактов к USDT, `onboardDate` у всех;
  делистнутые остаются в списке со статусом `SETTLING`, дата запуска у них тоже есть;
* `api/v3/exchangeInfo` (вес 20) — спотовые пары, но ДАТЫ ЛИСТИНГА в ответе нет;
* `api/v3/klines?startTime=0&limit=1` (вес 2) — первая дневная свеча пары. Её день и есть
  день листинга: ровно то определение, на котором мерили эффект (`listing_effect.py`).

Имена — по ccxt. 28.09 сверено: у всех 658 перпов символ ccxt равен `<baseAsset>/USDT:USDT`,
включая имена из иероглифов (`牛来/USDT:USDT`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

FAPI_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SPOT_INFO = "https://api.binance.com/api/v3/exchangeInfo"
SPOT_KLINES = "https://api.binance.com/api/v3/klines"
QUOTE = "USDT"


def perp_symbol(base: str) -> str:
    """Бессрочный контракт монеты к USDT по имени ccxt."""
    return f"{base}/{QUOTE}:{QUOTE}"


def _day(ms: Any) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, UTC).date()


class BinanceListings:
    venue = "binance"

    def __init__(self, transport: Any = None) -> None:
        if transport is None:
            from lab.feeds.chains.transport import HttpxTransport

            transport = HttpxTransport()
        self.transport = transport

    def perps(self) -> dict[str, date]:
        """База → день запуска бессрочного контракта к USDT, включая делистнутые.

        Делистнутые нужны: правило «перп был к входу» смотрит на прошлое, а не на сегодня.
        """
        out: dict[str, date] = {}
        for s in self.transport.get(FAPI_INFO).get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != QUOTE:
                continue
            if s.get("onboardDate"):
                out[str(s["baseAsset"])] = _day(s["onboardDate"])
        return out

    def spot_pairs(self) -> dict[str, str]:
        """База → символ спотовой пары к USDT, которая торгуется сейчас.

        Пары в `BREAK` (213 из 709 на 28.09) — давно снятые; новая пара появляется сразу
        в `TRADING`. Символ берётся из ответа, а не склеивается: так надёжнее для любых имён.
        """
        data = self.transport.get(SPOT_INFO, params={"permissions": "SPOT"})
        return {
            str(s["baseAsset"]): str(s["symbol"])
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == QUOTE and s.get("status") == "TRADING"
        }

    def first_day(self, symbol: str) -> date | None:
        """День первой дневной свечи пары — день листинга. Свечей ещё нет — None."""
        rows = self.transport.get(
            SPOT_KLINES,
            params={"symbol": symbol, "interval": "1d", "startTime": 0, "limit": 1},
        )
        if not isinstance(rows, list) or not rows:
            return None
        return _day(rows[0][0])


__all__ = ["FAPI_INFO", "SPOT_INFO", "SPOT_KLINES", "BinanceListings", "perp_symbol"]
