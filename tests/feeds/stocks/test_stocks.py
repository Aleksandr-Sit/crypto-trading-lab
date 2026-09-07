"""Свечи акций (G01.2, История 85a): провайдер по `STOCK_DATA_PROVIDER`,
тикеры из `config/stocks.yaml`,
свечи в Parquet через `CandleStore`; стратегия `asset_class: stock` — сигнал без исполнения."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.data import CandleStore
from lab.feeds.stocks import AlphaVantageProvider, StockFeed, load_stocks, make_stock_provider
from lab.strategies.stocks import is_signal_only, make_stock_strategy

AV_SAMPLE = {
    "Meta Data": {"2. Symbol": "AAPL"},
    "Time Series (Daily)": {
        "2026-09-02": {
            "1. open": "230.0",
            "2. high": "232.0",
            "3. low": "229.0",
            "4. close": "231.5",
            "5. volume": "1000",
        },
        "2026-09-01": {
            "1. open": "228.0",
            "2. high": "231.0",
            "3. low": "227.5",
            "4. close": "230.0",
            "5. volume": "900",
        },
    },
}


def test_provider_from_env_unavailable_without_setting_and_key():
    p = make_stock_provider({})
    assert p.health().status == "down" and "недоступен" in p.health().detail
    assert (
        p.candles("AAPL", "1d", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC))
        == []
    )
    p = make_stock_provider({"STOCK_DATA_PROVIDER": "alphavantage"})
    assert p.health().status == "down" and "ALPHAVANTAGE_API_KEY" in p.health().detail
    assert make_stock_provider({"STOCK_DATA_PROVIDER": "yfinance"}).name == "yfinance"


def test_alphavantage_parses_daily_series_and_feed_writes_parquet(tmp_path):
    provider = AlphaVantageProvider("key", fetch=lambda url: json.dumps(AV_SAMPLE))
    cfg = load_stocks()
    assert "AAPL" in cfg.tickers and cfg.timeframe == "1d"
    store = CandleStore(tmp_path)
    feed = StockFeed(provider, store)
    t0, t1 = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
    report = feed.sync(["AAPL"], "1d", t0, t1)
    assert report["AAPL"].rows_written == 2
    rows = feed.candles("AAPL", "1d", t0, t1)
    assert [r.close for r in rows] == [Decimal("230.0"), Decimal("231.5")]
    assert store.count("stocks", "AAPL", "1d") == 2


def test_stock_strategy_emits_signal_only_without_execution():
    strategy = make_stock_strategy(
        "cex-spot-indicator-pifagor-forever-sma-v0",
        tickers=["AAPL"],
        params={"sma_period_days": 5, "min_bars": 6},
    )
    m = strategy.manifest
    assert m.params["asset_class"] == "stock" and m.branch == "rh" and m.instruments == ["AAPL"]
    assert is_signal_only(m) and strategy.strategy_id == "rh-indicator-pifagor-forever-sma-v0"
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    signals = []
    for i, c in enumerate([100, 100, 100, 100, 100, 100, 100, 100, 120, 125]):
        d = Decimal(c)
        from lab.contracts import Candle

        signals += strategy.on_bar(
            Candle(
                instrument="AAPL",
                tf="1d",
                ts=t0 + timedelta(days=i),
                open=d,
                high=d,
                low=d,
                close=d,
                volume=Decimal(1000),
            )
        )
    assert (
        signals
        and signals[-1].meta["asset_class"] == "stock"
        and signals[-1].meta["execution"] == "manual"
    )


class _Idx:
    """Подделка под `pandas.Index`; `levels` есть только у мультииндекса колонок."""

    def __init__(self, multi: bool) -> None:
        if multi:
            self.levels = [["Open"], ["AAPL"]]


class _Row:
    def __init__(self, values: dict, *, multi: bool, ticker: str = "AAPL") -> None:
        self._values = values
        self._multi = multi
        self._ticker = ticker
        self.index = _Idx(multi)

    def __getitem__(self, key):
        if self._multi:
            field, ticker = key
            assert ticker == self._ticker
            return self._values[field]
        return self._values[key]


class _Ts:
    def __init__(self, ts: datetime) -> None:
        self._ts = ts

    def to_pydatetime(self) -> datetime:
        return self._ts


class _Frame:
    def __init__(self, rows: list[tuple[datetime, dict]], *, multi: bool) -> None:
        self._rows = rows
        self._multi = multi

    def iterrows(self):
        for ts, values in self._rows:
            yield _Ts(ts), _Row(values, multi=self._multi)


def _bar(close: float) -> dict:
    return {
        "Open": close - 1,
        "High": close + 1,
        "Low": close - 2,
        "Close": close,
        "Volume": 1000,
    }


def test_yfinance_rows_keep_their_own_values() -> None:
    """Каждая свеча берёт значения своей строки фрейма, а не последней (B023)."""
    from lab.feeds.stocks.feed import YfinanceProvider

    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        (t0, _bar(100.0)),
        (t0 + timedelta(days=1), _bar(200.0)),
        (t0 + timedelta(days=2), _bar(300.0)),
    ]
    for multi in (False, True):
        frame = _Frame(rows, multi=multi)
        provider = YfinanceProvider(download=lambda *a, _frame=frame, **k: _frame)
        candles = provider.candles("AAPL", "1d", t0, t0 + timedelta(days=3))
        assert [c.close for c in candles] == [Decimal("100"), Decimal("200"), Decimal("300")]
        assert [c.open for c in candles] == [Decimal("99"), Decimal("199"), Decimal("299")]
