"""Исполнители CEX и Hyperliquid (таск 04). Импорт пакета регистрирует четыре площадки.

Фабрика `make_executor(venue)` без аргументов — как требует `executors.registry`:
транспорт — ccxt с ключами из `.env` (пустой ключ → «только данные»: paper работает,
live поднимает NotConnected). Офлайн-транспорт (`FakeTransport`) подставляется только по
явной `LAB_CEX_TRANSPORT=fake` — её выставляет `tests/conftest.py`, так контрактный тест
исполнителей проходит без сети (ticket 04: «фейковый транспорт ccxt»).
"""

from __future__ import annotations

import os
from decimal import Decimal

from lab.contracts import ModeLiteral
from lab.executors import registry
from lab.executors.cex.executor import (
    ACTIVE_STATES,
    EXECUTORS,
    BinanceExecutor,
    BybitExecutor,
    CexError,
    CexExecutor,
    FundingPayment,
    HyperliquidExecutor,
    KeyRejected,
    NotConnected,
    OkxExecutor,
    PaperFill,
    PerpInfo,
    ReconcileResult,
    client_order_id,
)
from lab.feeds import QuotaSink
from lab.feeds.cex.transport import VENUES, Transport


def offline_requested(env: dict[str, str] | None = None) -> bool:
    """Фейковый транспорт — только по явной `LAB_CEX_TRANSPORT=fake` (tests/conftest.py)."""
    env = os.environ if env is None else env
    return env.get("LAB_CEX_TRANSPORT", "").lower() == "fake"


def make_transport(venue: str) -> Transport:
    if offline_requested():
        from lab.feeds.cex.fake import FakeTransport

        return FakeTransport(venue, has_keys=False, default_mid=Decimal("100"))
    from lab.feeds.cex.transport import CcxtTransport

    return CcxtTransport(venue)


def make_executor(
    venue: str,
    transport: Transport | None = None,
    *,
    mode: ModeLiteral = "paper",
    quota: QuotaSink | None = None,
) -> CexExecutor:
    return EXECUTORS[venue](transport or make_transport(venue), mode=mode, quota=quota)


for _venue in VENUES:
    registry.register(_venue, (lambda v: lambda: make_executor(v))(_venue), replace=True)

__all__ = [
    "ACTIVE_STATES",
    "EXECUTORS",
    "BinanceExecutor",
    "BybitExecutor",
    "CexError",
    "CexExecutor",
    "FundingPayment",
    "HyperliquidExecutor",
    "KeyRejected",
    "NotConnected",
    "OkxExecutor",
    "PaperFill",
    "PerpInfo",
    "ReconcileResult",
    "client_order_id",
    "make_executor",
    "make_transport",
    "offline_requested",
]
