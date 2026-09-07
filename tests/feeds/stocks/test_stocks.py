"""Свечи акций (G01.2, История 85a): провайдер по `STOCK_DATA_PROVIDER`, тикеры из `config/stocks.yaml`,
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
