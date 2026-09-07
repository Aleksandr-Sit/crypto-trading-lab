"""Отбор кошельков (Истории 57–59): пересчёт по сделкам кошелька, флаги накрутки, цена задержки.

Ожидаемые значения посчитаны руками по синтетической истории ниже, а не тем же кодом.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds.chains import WalletTrade
from lab.wallets import Flag, WalletStats, flags, lag_cost, load_stats, recalc, save_stats

CHAIN = "solana"
ADDR = "So1Wa11et1111111111111111111111111111111111"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _swap(token, side, qty, quote_qty, ts, *, address=ADDR, chain=CHAIN) -> WalletTrade:
    return WalletTrade(
        chain=chain,
        address=address,
        tx=f"{token}-{side}-{ts.isoformat()}",
        ts=ts,
        token=token,
        symbol=token,
        side=side,
        qty=Decimal(qty),
        quote_asset="USDC",
        quote_qty=Decimal(quote_qty),
        value_usd=Decimal(quote_qty),
    )


def _round_trip(token: str, day: int, sold_for: str) -> list[WalletTrade]:
    """Покупка на 1000 USDC и полная продажа через сутки — цена входа всегда 1.0."""
    buy = _swap(token, "buy", "1000", "1000", T0 + timedelta(days=day))
    sell = _swap(token, "sell", "1000", sold_for, T0 + timedelta(days=day + 1))
    return [buy, sell]


# PnL по токенам: A +2000, B −300, C +200, D −600, E −100, F −950 (F обнулился — −95%)
HISTORY = (
    _round_trip("A", 0, "3000")
    + _round_trip("B", 2, "700")
    + _round_trip("C", 4, "1200")
    + _round_trip("D", 6, "400")
    + _round_trip("E", 8, "900")
    + _round_trip("F", 10, "50")
)


class _FeedStub:
    """Стенд вместо сетевого клиента: `recalc` считает по сделкам, которые ей отдали."""

    def __init__(self, trades):
        self.trades = trades
        self.asked: list[str] = []

    def wallet_trades(self, address, from_ts=None, to_ts=None, **kw):
        self.asked.append(address)
        return list(self.trades)


def test_recalc_counts_by_wallet_trades_not_by_showcase():
    feed = _FeedStub(HISTORY)

    stats = recalc(ADDR, CHAIN, feed=feed, now=NOW)

    assert feed.asked == [ADDR]
    assert stats.n_trades == 6  # шесть закрытых кругов
    assert stats.win_rate_pct == Decimal("33.33")  # прибыльны A и C
    assert stats.median_pnl == Decimal("-200")  # медиана (−300, −100)
    assert stats.pnl_total == Decimal("250")
    # кривая капитала 1000 → 3000 (пик) → 1250 (дно): просадка 1750/3000
    assert stats.max_dd_pct == Decimal("58.33")
    assert stats.age_days == 151  # первая сделка 2026-01-01, «сейчас» 2026-06-01
    assert stats.survived_pct == Decimal("83.33")  # пять токенов из шести не обнулились
    assert stats.currency == "USD"


def test_flags_mark_single_luck_and_too_young():
    stats = recalc(ADDR, CHAIN, trades=HISTORY, now=NOW)

    codes = {f.code for f in flags(ADDR, CHAIN, trades=HISTORY, stats=stats)}

    # лучшая сделка (+2000) — 90% всей прибыли; сделок 6 при пороге 30
    assert codes == {"single_luck", "too_young"}
    assert all(isinstance(f, Flag) and f.detail for f in flags(ADDR, CHAIN, trades=HISTORY))


def test_flag_wash_trading_on_fast_round_trips():
    trades: list[WalletTrade] = []
    for i in range(10):
        ts = T0 + timedelta(days=i)
        trades.append(_swap("W", "buy", "1000", "1000", ts))
        trades.append(_swap("W", "sell", "1000", "1000", ts + timedelta(seconds=60)))

    codes = {f.code for f in flags(ADDR, CHAIN, trades=trades)}

    assert "wash_trading" in codes


def test_flag_own_token_on_single_token_dominance():
    trades = _round_trip("OWN", 0, "1200") * 4 + _round_trip("OTHER", 20, "1000")

    codes = {f.code for f in flags(ADDR, CHAIN, trades=trades)}

    assert "own_token" in codes


def test_lag_cost_over_three_delays():
    trades = [_swap("A", "buy", "1000", "1000", T0)]  # цена входа лидера — 1.0
    later = {5: Decimal("1.01"), 30: Decimal("1.03"), 120: Decimal("0.98")}

    def price(token, ts):
        return later[int((ts - T0).total_seconds())]

    cost = lag_cost(ADDR, CHAIN, trades=trades, price=price)

    assert sorted(cost.by_delay_s) == [5, 30, 120]
    assert cost.by_delay_s[5] == Decimal("100")  # +1% к цене входа = 100 б.п. потери
    assert cost.by_delay_s[30] == Decimal("300")
    assert cost.by_delay_s[120] == Decimal("-200")  # цена упала — повтор дешевле
    assert cost.samples[5] == 1


def test_stats_and_flags_are_stored_per_wallet(session):
    stats = recalc(ADDR, CHAIN, trades=HISTORY, now=NOW)
    marks = flags(ADDR, CHAIN, trades=HISTORY, stats=stats)

    save_stats(session, stats, marks)
    session.flush()
    loaded, loaded_flags = load_stats(session, ADDR, CHAIN)

    assert isinstance(loaded, WalletStats)
    assert loaded.n_trades == stats.n_trades and loaded.win_rate_pct == stats.win_rate_pct
    assert {f.code for f in loaded_flags} == {f.code for f in marks}
