"""Порог В12 на границах (R12, R12.1, R11.1, A04) и метрики на разобранном вручную примере."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Costs
from lab.core.measure import ClosedTrade, NotApplicable, metrics, threshold

T0 = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = (T0, T0 + timedelta(days=30))


def _trade(i: int, pnl: str, fee: str = "1") -> ClosedTrade:
    return ClosedTrade(
        instrument="SYN/USD",
        side="long",
        qty=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(100) + Decimal(pnl),
        opened_at=T0 + timedelta(hours=12 * i),
        closed_at=T0 + timedelta(hours=12 * i + 6),
        pnl_gross=Decimal(pnl),
        costs=Costs(fee=Decimal(fee)),
    )


def _trades(n: int, pnl: str = "5") -> list[ClosedTrade]:
    return [_trade(i, pnl) for i in range(n)]


def test_hand_checked_metrics():
    # Три сделки: +10, −4, +2 при комиссии 1 каждая → net = 9−5+1 = 5.
    trades = [_trade(0, "10"), _trade(1, "-4"), _trade(2, "2")]
    m = metrics(trades, Decimal("2"), capital=Decimal(1000), window=WINDOW)
    assert m.n_trades == 3
    assert m.net_pnl == Decimal(5)
    assert m.net_pnl_pct == Decimal("0.5")
    assert m.ev_per_trade == Decimal(5) / 3
    assert m.win_rate == Decimal(2) / 3
    assert m.profit_factor == Decimal(10) / 5  # gross profit 9+1 / gross loss 5
    assert m.payoff == Decimal(5) / 5  # средняя прибыль 5 / средний убыток 5
    # Просадка: пик после первой (1009), дно после второй (1004) → 5/1009 %.
    assert m.max_dd_pct == Decimal(5) / Decimal(1009) * 100
    assert m.dd_duration_days == Decimal(1)  # пик 06:00 1-го → восстановление 06:00 2-го
    assert m.costs.fee == Decimal(3) and m.costs.total == Decimal(3)
    assert m.costs_pct == Decimal(3) / Decimal(608) * 100  # оборот: входы 300 + выходы 308
    assert m.vs_benchmark == Decimal("0.5") - Decimal("2")
    assert m.sample.detail == "недостаточно данных: 3 из 30"
    assert isinstance(m.copy_lag_cost, NotApplicable) and "copy" in m.copy_lag_cost.reason
    assert m.ev_ci95 is not None and m.ev_ci95[0] <= m.ev_per_trade <= m.ev_ci95[1]
    # Тот же набор — тот же интервал (детерминированный бутстрап).
    assert metrics(trades, None, capital=Decimal(1000), window=WINDOW).ev_ci95 == m.ev_ci95
    assert m.exposure_pct == Decimal("2.5")  # 3 × 6 ч из 720 ч


def test_zero_trades_is_insufficient_not_zeros():
    m = metrics([], Decimal(0), capital=Decimal(1000), window=WINDOW)
    assert m.sample.status == "insufficient"
    assert m.sample.detail == "недостаточно данных: 0 из 30"
    assert m.ev_per_trade is None and m.win_rate is None and m.ev_ci95 is None
    t = threshold(m, "cex-spot")
    assert t.status == "insufficient"


def test_threshold_boundaries():
    # 29 сделок — insufficient; 30 — считается.
    assert (
        threshold(
            metrics(_trades(29), Decimal(0), capital=Decimal(1000), window=WINDOW), "cex-spot"
        ).status
        == "insufficient"
    )
    ok = threshold(
        metrics(_trades(30), Decimal(0), capital=Decimal(1000), window=WINDOW), "cex-spot"
    )
    assert ok.status == "passed"
    by_name = {c.name: c for c in ok.criteria}
    assert by_name["n_trades"].value == 30 and by_name["n_trades"].limit == 30
    assert by_name["max_dd_pct"].limit == Decimal(5)  # группа cex
    assert by_name["vs_benchmark"].passed is True

    # EV ровно 0 после издержек (pnl 1, fee 1) — не проходит (нужно строго > 0).
    zero = threshold(
        metrics(_trades(30, "1"), Decimal(-1), capital=Decimal(1000), window=WINDOW), "cex-spot"
    )
    assert zero.status == "failed" and zero.failed_names() == ["ev_per_trade"]

    # Хуже BTC: net 120/1000 = 12% против BTC 12% ровно → vs_benchmark = 0 → не пройден.
    worse = threshold(
        metrics(_trades(30), Decimal(12), capital=Decimal(1000), window=WINDOW), "cex-spot"
    )
    assert worse.failed_names() == ["vs_benchmark"]

    # Просадка ровно на лимите (5%) проходит; чуть больше — нет (группа meme: лимит 20).
    trades = _trades(30) + [_trade(30, "-60")]  # пик 1120 → дно 1059: 61/1120 = 5.45%
    dd = threshold(metrics(trades, Decimal(0), capital=Decimal(1000), window=WINDOW), "cex-spot")
    assert dd.failed_names() == ["max_dd_pct"]
    assert (
        threshold(metrics(trades, Decimal(0), capital=Decimal(1000), window=WINDOW), "meme").status
        == "passed"
    )


def test_auto_rung_requires_ci_low_above_zero():
    # Половина +5, половина −4.5 при fee 1: EV = (4 − 5.5)/2 < 0... сделаем EV > 0, CI низ < 0.
    trades = [_trade(i, "5" if i % 2 else "-3") for i in range(30)]
    m = metrics(trades, Decimal(0), capital=Decimal(1000), window=WINDOW)
    assert m.ev_per_trade == Decimal(0)  # (4·15 − 4·15)/30
    trades = [_trade(i, "6" if i % 2 else "-3") for i in range(30)]
    m = metrics(trades, Decimal(0), capital=Decimal(1000), window=WINDOW)
    assert m.ev_per_trade > 0 and m.ev_ci95[0] < 0
    assert threshold(m, "cex-spot", rung="semi").status == "passed"
    auto = threshold(m, "cex-spot", rung="auto")
    assert auto.status == "failed" and auto.failed_names() == ["ev_ci95_low"]


def test_missing_benchmark_is_insufficient_not_failed():
    # Бенчмарка за окно нет: vs_benchmark посчитать не из чего — это нехватка данных, а не провал.
    m = metrics(_trades(30), None, capital=Decimal(1000), window=WINDOW)
    assert m.vs_benchmark is None
    t = threshold(m, "cex-spot")
    assert t.status == "insufficient"
    assert t.failed_names() == []
    by_name = {c.name: c for c in t.criteria}
    assert by_name["vs_benchmark"].passed is None
    assert "нет данных бенчмарка за окно" in by_name["vs_benchmark"].detail
    assert by_name["n_trades"].passed is True  # выборки хватает, не хватает именно бенчмарка
