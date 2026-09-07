"""Исполнитель Polymarket. Импорт регистрирует его в `executors.registry` (paper по умолчанию)."""

import os

from lab.contracts import ModeLiteral
from lab.executors import registry
from lab.executors.polymarket.executor import (
    BRANCH,
    QUOTE_ASSET,
    VENUE,
    NotConnected,
    PaperFill,
    PolymarketExecutor,
    TradingAccess,
    TradingUnavailable,
    check_trading_access,
)
from lab.feeds.polymarket import PolymarketError, PolymarketFeed
from lab.feeds.quota import QuotaSink

KEY_ENV = "POLYMARKET_PRIVATE_KEY"


def make_executor(
    mode: ModeLiteral = "paper",
    *,
    feed: PolymarketFeed | None = None,
    quota: QuotaSink | None = None,
    private_key: str | None = None,
    env: dict[str, str] | None = None,
) -> PolymarketExecutor:
    """Ключ подписи — только из окружения по имени из `.env.example`."""
    source = os.environ if env is None else env
    return PolymarketExecutor(
        feed,
        mode=mode,
        quota=quota,
        private_key=private_key or source.get(KEY_ENV) or None,
    )


registry.register("polymarket", lambda: make_executor("paper"), replace=True)

__all__ = [
    "BRANCH",
    "KEY_ENV",
    "QUOTE_ASSET",
    "VENUE",
    "NotConnected",
    "PaperFill",
    "PolymarketError",
    "PolymarketExecutor",
    "TradingAccess",
    "TradingUnavailable",
    "check_trading_access",
    "make_executor",
]
