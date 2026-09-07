"""`lag_cost` — во сколько обходится повтор сделки лидера через 5/30/120 с (История 59).

Считается по ценам после сделки лидера: положительные базисные пункты — потеря
(покупка дороже, продажа дешевле), отрицательные — повтор оказался выгоднее.
Источник цен — функция `(token, ts) -> цена | None`: у неё может не быть точки на этот
момент, тогда сделка в выборку задержки не входит (и это видно в `samples`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from lab.feeds.chains.config import wallet_thresholds
from lab.feeds.chains.types import WalletTrade
from lab.wallets.types import LagCost

BPS = Decimal(10_000)
CENTS = Decimal("0.01")

PriceAt = Callable[[str, object], Decimal | None]


def lag_cost(
    address: str,
    chain: str,
    *,
    trades: Sequence[WalletTrade],
    price: PriceAt,
    delays_s: Sequence[int] | None = None,
) -> LagCost:
    delays = list(delays_s or wallet_thresholds().lag_delays_s)
    totals: dict[int, Decimal] = {d: Decimal(0) for d in delays}
    counts: dict[int, int] = {d: 0 for d in delays}
    for trade in trades:
        if trade.price <= 0:
            continue
        for delay in delays:
            later = price(trade.token, trade.ts + timedelta(seconds=delay))
            if later is None:
                continue
            drift = (Decimal(str(later)) - trade.price) / trade.price * BPS
            totals[delay] += drift if trade.side == "buy" else -drift
            counts[delay] += 1
    by_delay = {
        d: (totals[d] / counts[d]).quantize(CENTS, rounding=ROUND_HALF_UP)
        for d in delays
        if counts[d]
    }
    return LagCost(
        address=address,
        chain=chain,
        by_delay_s=by_delay,
        samples={d: counts[d] for d in delays},
        n_trades=len(trades),
    )


__all__ = ["lag_cost"]
