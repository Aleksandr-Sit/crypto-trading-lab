"""Четыре флага накрутки (История 58). Пороги — `config/chains.yaml`, раздел `wallets`.

Флаг не приговор: отсеянные кошельки остаются видимы отдельно, вместе с причиной.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from lab.feeds.chains.config import WalletThresholds, wallet_thresholds
from lab.feeds.chains.types import WalletTrade
from lab.wallets.stats import close_trades, merge_by_position, recalc
from lab.wallets.types import Flag, WalletStats

WASH_TRADING = "wash_trading"
OWN_TOKEN = "own_token"
SINGLE_LUCK = "single_luck"
TOO_YOUNG = "too_young"
CENTS = Decimal("0.01")


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    if whole <= 0:
        return Decimal(0)
    return (part / whole * 100).quantize(CENTS, rounding=ROUND_HALF_UP)


def flags(
    address: str,
    chain: str,
    *,
    trades: Sequence[WalletTrade],
    stats: WalletStats | None = None,
    thresholds: WalletThresholds | None = None,
    own_tokens: Sequence[str] = (),
) -> list[Flag]:
    """Флаги кошелька. `own_tokens` — токены, где кошелёк известен как создатель."""
    limits = thresholds or wallet_thresholds()
    stats = stats or recalc(address, chain, trades=trades)
    found: list[Flag] = []

    round_trips = _fast_round_trips(trades, limits.wash_round_trip_s)
    wash_pct = _pct(Decimal(round_trips), Decimal(max(stats.n_trades, 1)))
    if round_trips and wash_pct >= limits.wash_share_pct:
        found.append(
            Flag(
                code=WASH_TRADING,
                detail=(
                    f"{wash_pct}% кругов закрыты быстрее "
                    f"{limits.wash_round_trip_s} с — похоже на прокрутку объёма"
                ),
                value=wash_pct,
            )
        )

    volume: dict[str, Decimal] = defaultdict(Decimal)
    for trade in trades:
        volume[trade.token] += trade.value
    total = sum(volume.values(), Decimal(0))
    if volume:
        token, share_value = max(volume.items(), key=lambda kv: kv[1])
        share = _pct(share_value, total)
        if share >= limits.own_token_share_pct or token in set(own_tokens):
            found.append(
                Flag(
                    code=OWN_TOKEN,
                    detail=(
                        f"{share}% оборота — токен {token}: "
                        "результат кошелька держится на одном своём активе"
                    ),
                    value=share,
                )
            )

    closed = merge_by_position(close_trades(trades)[0])
    profits = [t.pnl for t in closed if t.pnl > 0]
    if profits:
        share = _pct(max(profits), sum(profits, Decimal(0)))
        if share >= limits.single_luck_share_pct:
            found.append(
                Flag(
                    code=SINGLE_LUCK,
                    detail=f"{share}% всей прибыли дала одна сделка — единичная удача",
                    value=share,
                )
            )

    if stats.age_days < limits.min_age_days or stats.n_trades < limits.min_trades:
        found.append(
            Flag(
                code=TOO_YOUNG,
                detail=(
                    f"возраст {stats.age_days} дн. при пороге {limits.min_age_days}, "
                    f"сделок {stats.n_trades} при пороге {limits.min_trades}"
                ),
                value=Decimal(stats.n_trades),
            )
        )
    return found


def _fast_round_trips(trades: Sequence[WalletTrade], window_s: int) -> int:
    closed = merge_by_position(close_trades(trades)[0])
    return sum(1 for t in closed if (t.closed_at - t.opened_at).total_seconds() <= window_s)


__all__ = ["OWN_TOKEN", "SINGLE_LUCK", "TOO_YOUNG", "WASH_TRADING", "flags"]
