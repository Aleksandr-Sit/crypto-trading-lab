"""Провайдеры дневных свечей акций и фид поверх `CandleStore`.

- `YfinanceProvider` — без ключа (`yfinance`, импорт ленивый; `download=` подменяется в тестах);
- `AlphaVantageProvider(api_key, fetch=)` — `TIME_SERIES_DAILY` (бесплатный тариф: 25 запросов/день);
- `NoStockProvider` — «недоступен» с причиной, не падает.
Выбор — `make_stock_provider(env)` по `STOCK_DATA_PROVIDER` (yfinance | alphavantage)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.request import urlopen

from pydantic import BaseModel, Field

from lab.config import CONFIG_DIR, load_config
from lab.contracts import Candle, Health
from lab.data import CandleStore, WriteResult

Fetch = Callable[[str], str]


class StocksConfig(BaseModel):
    version: int = 1
    timeframe: str = "1d"
    venue: str = "stocks"
    tickers: list[str] = Field(default_factory=list)


def load_stocks(path: Path | str | None = None) -> StocksConfig:
    return load_config(path or CONFIG_DIR / "stocks.yaml", StocksConfig)


class StockProvider(Protocol):
    name: str

    def candles(self, ticker: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]: ...
    def health(self) -> Health: ...


def _d(v) -> Decimal:
    return Decimal(str(v))


class NoStockProvider:
    name = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def candles(self, ticker: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        return []

    def health(self) -> Health:
        return Health(
            status="down", detail=f"недоступен: {self.reason}", checked_at=datetime.now(UTC)
        )


class AlphaVantageProvider:
    name = "alphavantage"
    URL = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={ticker}&outputsize={size}&apikey={key}"

    def __init__(self, api_key: str, *, fetch: Fetch | None = None) -> None:
        self.api_key, self._fetch = api_key, fetch

    def _get(self, url: str) -> str:
        if self._fetch is not None:
            return self._fetch(url)
        with urlopen(url, timeout=30) as resp:  # noqa: S310 — фиксированный https-хост
            return resp.read().decode()

    def candles(self, ticker: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        if tf != "1d":
            raise ValueError("Alpha Vantage free: только дневные свечи")
        size = "compact" if datetime.now(UTC) - from_ts < timedelta(days=100) else "full"
        data = json.loads(self._get(self.URL.format(ticker=ticker, size=size, key=self.api_key)))
        series = data.get("Time Series (Daily)") or {}
        out = []
        for day, row in series.items():
            ts = datetime.fromisoformat(day).replace(tzinfo=UTC)
            if from_ts <= ts < to_ts:
                out.append(
                    Candle(
                        instrument=ticker,
                        tf="1d",
                        ts=ts,
                        open=_d(row["1. open"]),
                        high=_d(row["2. high"]),
                        low=_d(row["3. low"]),
                        close=_d(row["4. close"]),
                        volume=_d(row["5. volume"]),
                    )
                )
        return sorted(out, key=lambda c: c.ts)

    def health(self) -> Health:
        return Health(status="ok", detail="Alpha Vantage: ключ задан", checked_at=datetime.now(UTC))


class YfinanceProvider:
    name = "yfinance"
    _INTERVAL = {"1d": "1d", "1h": "1h", "1w": "1wk"}

    def __init__(self, *, download: Callable | None = None) -> None:
        self._download = download

    def candles(self, ticker: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        download = self._download
        if download is None:
            import yfinance as yf  # ленивый импорт: без сети/пакета фид просто недоступен

            download = yf.download
        frame = download(
            ticker,
            start=from_ts.date().isoformat(),
            end=to_ts.date().isoformat(),
            interval=self._INTERVAL.get(tf, "1d"),
            progress=False,
            auto_adjust=False,
        )
        out = []
        for idx, row in frame.iterrows():
            ts = idx.to_pydatetime()
            ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
            get = (
                (lambda k: row[(k, ticker)]) if hasattr(row.index, "levels") else (lambda k: row[k])
            )
            out.append(
                Candle(
                    instrument=ticker,
                    tf=tf,
                    ts=ts,
                    open=_d(get("Open")),
                    high=_d(get("High")),
                    low=_d(get("Low")),
                    close=_d(get("Close")),
                    volume=_d(int(get("Volume"))),
                )
            )
        return out

    def health(self) -> Health:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return Health(
                status="down",
                detail="недоступен: пакет yfinance не установлен",
                checked_at=datetime.now(UTC),
            )
        return Health(status="ok", detail="yfinance (без ключа)", checked_at=datetime.now(UTC))


def make_stock_provider(env: Mapping[str, str] | None = None) -> StockProvider:
    env = os.environ if env is None else env
    kind = (env.get("STOCK_DATA_PROVIDER") or "").strip().lower()
    if kind == "yfinance":
        return YfinanceProvider()
    if kind == "alphavantage":
        key = env.get("ALPHAVANTAGE_API_KEY")
        return AlphaVantageProvider(key) if key else NoStockProvider("нет ALPHAVANTAGE_API_KEY")
    if not kind:
        return NoStockProvider("STOCK_DATA_PROVIDER не задан (yfinance | alphavantage)")
    return NoStockProvider(f"неизвестный STOCK_DATA_PROVIDER={kind!r}")


class StockFeed:
    """Фид акций: `sync` тянет свечи у провайдера в Parquet, `candles` читает из хранилища."""

    def __init__(
        self, provider: StockProvider, store: CandleStore, *, venue: str = "stocks"
    ) -> None:
        self.provider, self.store, self.venue = provider, store, venue

    def sync(
        self, tickers: Sequence[str], tf: str, from_ts: datetime, to_ts: datetime
    ) -> dict[str, WriteResult]:
        report: dict[str, WriteResult] = {}
        for ticker in tickers:
            rows = self.provider.candles(ticker, tf, from_ts, to_ts)
            report[ticker] = self.store.write(self.venue, ticker, tf, rows)
        return report

    def candles(self, ticker: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        return self.store.read(self.venue, ticker, tf, from_ts, to_ts)

    def source(self):
        """Источник для `core.measure.run(source=...)` — читает из хранилища."""
        return self.candles

    def health(self) -> Health:
        return self.provider.health()


__all__ = [
    "AlphaVantageProvider",
    "NoStockProvider",
    "StockFeed",
    "StockProvider",
    "StocksConfig",
    "YfinanceProvider",
    "load_stocks",
    "make_stock_provider",
]
