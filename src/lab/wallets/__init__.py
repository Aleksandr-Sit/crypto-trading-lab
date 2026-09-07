"""Отбор кошельков и лидеров (Истории 57–59, 63).

`recalc` считает результат по сделкам кошелька, `flags` ставит четыре признака накрутки,
`lag_cost` показывает цену задержки копирования. Индексация сделок спрятана в `feeds.chains`.
"""

from lab.wallets.flags import OWN_TOKEN, SINGLE_LUCK, TOO_YOUNG, WASH_TRADING, flags
from lab.wallets.lag import lag_cost
from lab.wallets.stats import ClosedTrade, close_trades, merge_by_position, recalc
from lab.wallets.store import load_stats, save_stats, tracked
from lab.wallets.types import Flag, LagCost, WalletStats

__all__ = [
    "OWN_TOKEN",
    "SINGLE_LUCK",
    "TOO_YOUNG",
    "WASH_TRADING",
    "ClosedTrade",
    "Flag",
    "LagCost",
    "WalletStats",
    "close_trades",
    "flags",
    "lag_cost",
    "load_stats",
    "merge_by_position",
    "recalc",
    "save_stats",
    "tracked",
]
