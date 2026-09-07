"""Исполнитель Robinhood. Импорт регистрирует его в `executors.registry` (paper по умолчанию)."""

from lab.contracts import ModeLiteral
from lab.executors import registry
from lab.executors.robinhood.executor import (
    BRANCH,
    QUOTE_ASSET,
    STOCK_VENUE,
    VENUE,
    NotConnected,
    PaperFill,
    RobinhoodExecutor,
    SignalOnly,
    TradingAccess,
    check_trading_access,
    is_crypto_pair,
    stock_signal_card,
)
from lab.feeds.quota import QuotaSink
from lab.feeds.robinhood import RobinhoodError, RobinhoodFeed, make_robinhood_feed


def make_executor(
    mode: ModeLiteral = "paper",
    *,
    feed: RobinhoodFeed | None = None,
    quota: QuotaSink | None = None,
    env: dict[str, str] | None = None,
) -> RobinhoodExecutor:
    return RobinhoodExecutor(feed, mode=mode, quota=quota)


registry.register("robinhood", lambda: make_executor("paper"), replace=True)

__all__ = [
    "BRANCH",
    "QUOTE_ASSET",
    "STOCK_VENUE",
    "VENUE",
    "NotConnected",
    "PaperFill",
    "RobinhoodError",
    "RobinhoodExecutor",
    "SignalOnly",
    "TradingAccess",
    "check_trading_access",
    "is_crypto_pair",
    "make_executor",
    "make_robinhood_feed",
    "stock_signal_card",
]
