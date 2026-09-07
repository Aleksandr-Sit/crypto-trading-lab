"""`recalc` — результат кошелька по его сделкам, а не по витрине (История 57).

Круг сделки собирается FIFO по каждому токену: покупка закрывается последующими
продажами того же токена. Всё, что осталось открытым, в PnL не входит — цены позиции
у нас нет, а домысливать её значит рисовать себе прибыль.
Кривая капитала стартует с пикового одновременно вложенного объёма: просадка считается
от неё, иначе первая же убыточная сделка даёт «просадку 100%».
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from lab.feeds.chains.types import STABLES, WalletTrade
from lab.wallets.types import WalletStats

RUG_SHARE = Decimal("0.1")  # вернулось меньше 10% вложенного — токен считаем обнулившимся
CENTS = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class ClosedTrade:
    """Закрытый круг по токену: сколько вложено, сколько вернулось, когда закрыт."""

    __slots__ = ("token", "cost", "proceeds", "opened_at", "closed_at")

    def __init__(self, token: str, cost: Decimal, proceeds: Decimal, opened_at, closed_at):
        self.token = token
        self.cost = cost
        self.proceeds = proceeds
        self.opened_at = opened_at
        self.closed_at = closed_at

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost


def close_trades(trades: Sequence[WalletTrade]) -> tuple[list[ClosedTrade], dict[str, Decimal]]:
    """FIFO по токенам: список закрытых кругов и остатки открытых позиций (в токене)."""
    lots: dict[str, deque[tuple[Decimal, Decimal, datetime]]] = defaultdict(deque)
    closed: list[ClosedTrade] = []
    for trade in sorted(trades, key=lambda t: t.ts):
        if trade.side == "buy":
            lots[trade.token].append((trade.qty, trade.value, trade.ts))
            continue
        left = trade.qty
        proceeds_rate = trade.value / trade.qty if trade.qty else Decimal(0)
        while left > 0 and lots[trade.token]:
            qty, cost, opened_at = lots[trade.token][0]
            take = min(qty, left)
            part_cost = cost * (take / qty) if qty else Decimal(0)
            closed.append(
                ClosedTrade(trade.token, part_cost, proceeds_rate * take, opened_at, trade.ts)
            )
            left -= take
            if take == qty:
                lots[trade.token].popleft()
            else:
                lots[trade.token][0] = (qty - take, cost - part_cost, opened_at)
    open_qty = {token: sum(q for q, _, _ in rest) for token, rest in lots.items() if rest}
    return closed, open_qty


def merge_by_position(closed: Sequence[ClosedTrade]) -> list[ClosedTrade]:
    """Части одной покупки, закрытые несколькими продажами, — это один круг, не три."""
    merged: dict[tuple[str, datetime], ClosedTrade] = {}
    for trade in closed:
        key = (trade.token, trade.opened_at)
        current = merged.get(key)
        if current is None:
            merged[key] = ClosedTrade(
                trade.token, trade.cost, trade.proceeds, trade.opened_at, trade.closed_at
            )
        else:
            current.cost += trade.cost
            current.proceeds += trade.proceeds
            current.closed_at = max(current.closed_at, trade.closed_at)
    return sorted(merged.values(), key=lambda t: t.closed_at)


def _currency(trades: Sequence[WalletTrade]) -> str:
    quotes = {t.quote_asset.upper() for t in trades}
    if not quotes or quotes <= STABLES or all(t.value_usd is not None for t in trades):
        return "USD"
    return "/".join(sorted(quotes))


def _peak_invested(trades: Sequence[WalletTrade]) -> Decimal:
    invested = Decimal(0)
    peak = Decimal(0)
    for trade in sorted(trades, key=lambda t: t.ts):
        invested += trade.value if trade.side == "buy" else -trade.value
        peak = max(peak, invested)
    return peak


def max_drawdown_pct(pnls: Sequence[Decimal], base: Decimal) -> Decimal:
    """Просадка кривой капитала `base + накопленный PnL`, % от достигнутого пика."""
    if not pnls or base <= 0:
        return Decimal(0)
    equity = base
    peak = base
    worst = Decimal(0)
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def recalc(
    address: str,
    chain: str,
    *,
    trades: Sequence[WalletTrade] | None = None,
    feed: object | None = None,
    source: Callable[[str, str], Sequence[WalletTrade]] | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    now: datetime | None = None,
) -> WalletStats:
    """Пересчёт кошелька. Сделки берутся у фида сети (или отдаются явно в `trades`)."""
    rows = list(_trades_of(address, chain, trades, feed, source, from_ts, to_ts))
    now = now or datetime.now(UTC)
    stats = WalletStats(address=address, chain=chain, computed_at=now)
    if not rows:
        return stats
    closed = merge_by_position(close_trades(rows)[0])
    open_qty = close_trades(rows)[1]
    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    tokens = {t.token for t in rows}
    rugged = {
        t.token
        for t in closed
        if t.cost > 0 and t.proceeds < t.cost * RUG_SHARE and t.token not in open_qty
    }
    stats = stats.model_copy(
        update={
            "n_trades": len(closed),
            "n_swaps": len(rows),
            "win_rate_pct": _round(Decimal(len(wins)) / len(pnls) * 100) if pnls else Decimal(0),
            "median_pnl": _round(_median(pnls)),
            "pnl_total": _round(sum(pnls, Decimal(0))),
            "best_pnl": _round(max(pnls)) if pnls else Decimal(0),
            "max_dd_pct": _round(max_drawdown_pct(pnls, _peak_invested(rows))),
            "age_days": (now - min(t.ts for t in rows)).days,
            "survived_pct": _round(
                Decimal(len(tokens) - len(rugged)) / Decimal(len(tokens)) * 100
            ),
            "tokens": len(tokens),
            "open_positions": len(open_qty),
            "volume": _round(sum((t.value for t in rows), Decimal(0))),
            "currency": _currency(rows),
            "first_trade_at": min(t.ts for t in rows),
            "last_trade_at": max(t.ts for t in rows),
        }
    )
    return stats


def _trades_of(address, chain, trades, feed, source, from_ts, to_ts) -> Sequence[WalletTrade]:
    if trades is not None:
        return trades
    if source is not None:
        return source(address, chain)
    if feed is None:
        from lab.feeds.chains import make_chain_feed

        feed = make_chain_feed(chain)
    return feed.wallet_trades(address, from_ts, to_ts)


__all__ = [
    "RUG_SHARE",
    "ClosedTrade",
    "close_trades",
    "max_drawdown_pct",
    "merge_by_position",
    "recalc",
]
