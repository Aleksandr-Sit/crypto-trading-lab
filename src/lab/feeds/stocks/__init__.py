"""Свечи акций (G01.2): провайдер по `STOCK_DATA_PROVIDER`, тикеры из `config/stocks.yaml`,
хранение в `CandleStore` (venue `stocks`)."""

from lab.feeds.stocks.feed import (
    AlphaVantageProvider,
    NoStockProvider,
    StockFeed,
    StockProvider,
    StocksConfig,
    YfinanceProvider,
    load_stocks,
    make_stock_provider,
)

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
