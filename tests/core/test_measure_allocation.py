"""Относительная планка просадки для правил РАЗМЕЩЕНИЯ (решение владельца 14.09.2026).

Правило, держащее 100% в активе, наследует просадку этого актива: фиксированные 5%
группы `cex` отбраковывали такой класс целиком — и ротацию золото/BTC (25.2% против
76.6% у самого биткойна), и накопительную лестницу. Планка стала относительной:
не больше доли от просадки бенчмарка за то же окно.

Главное, что здесь проверяется, — послабление НЕ бесплатное. Флаг в карточке без
измеренной просадки бенчмарка ничего не даёт, иначе он был бы способом обойти порог.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Costs
from lab.core.measure import ClosedTrade, metrics, threshold
from lab.core.measure.metrics import benchmark_drawdown_pct

T0 = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = (T0, T0 + timedelta(days=400))


def trade(i: int, pnl: str) -> ClosedTrade:
    return ClosedTrade(
        instrument="BTC/USDT",
        side="long",
        qty=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(100) + Decimal(pnl),
        opened_at=T0 + timedelta(days=i),
        closed_at=T0 + timedelta(days=i, hours=6),
        pnl_gross=Decimal(pnl),
        costs=Costs(fee=Decimal("0.1")),
    )


def candles(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            ts=T0 + timedelta(days=i),
            instrument="BTC/USDT",
            tf="1d",
            open=Decimal(str(closes[0])),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=Decimal(1),
        )
        for i, c in enumerate(closes)
    ]


# Бенчмарк проседает вдвое: 100 → 50 → 120. Просадка 50%.
DEEP = candles([100, 90, 50, 80, 120])


def sample(trades, bench, *, allocation: bool):
    m = metrics(
        trades,
        bench,
        capital=Decimal(1000),
        window=WINDOW,
        extra={"allocation": allocation, "benchmark_kind": "btc_dca"},
    )
    return m, threshold(m, "cex-spot")


def dd_of(result):
    return next(c for c in result.criteria if c.name == "max_dd_pct")


def test_benchmark_drawdown_is_measured():
    assert benchmark_drawdown_pct(DEEP) == Decimal(50)
    assert benchmark_drawdown_pct([]) is None


def test_allocation_gets_relative_limit():
    """Просадка 20% при 50% у бенчмарка — половина ровно, планка пройдена."""
    trades = [trade(0, "-200")] + [trade(i, "30") for i in range(1, 35)]
    m, result = sample(trades, DEEP, allocation=True)
    assert m.benchmark_max_dd_pct == Decimal(50)
    crit = dd_of(result)
    assert crit.limit == Decimal(25)  # половина от 50%
    assert "размещение" in crit.detail


def test_same_strategy_without_flag_is_judged_by_group_limit():
    """Без флага то же правило судится пятью процентами — и проваливается."""
    trades = [trade(0, "-200")] + [trade(i, "30") for i in range(1, 35)]
    _, with_flag = sample(trades, DEEP, allocation=True)
    _, without = sample(trades, DEEP, allocation=False)
    assert dd_of(with_flag).limit == Decimal(25)
    assert dd_of(without).limit == Decimal(5)
    assert dd_of(with_flag).passed and not dd_of(without).passed


def test_flag_without_benchmark_drawdown_gives_nothing():
    """Послабление не бесплатное: у бенчмарка «кэш» просадки нет, значит планка обычная.

    Иначе флаг в карточке стал бы способом обойти порог — а кэш-энд-керри с его 9.31%
    обязан оставаться провалившим.
    """
    trades = [trade(0, "-200")] + [trade(i, "30") for i in range(1, 35)]
    m = metrics(
        trades,
        None,  # бенчмарка нет вовсе
        capital=Decimal(1000),
        window=WINDOW,
        extra={"allocation": True, "benchmark_kind": "cash"},
    )
    assert m.benchmark_max_dd_pct is None
    crit = dd_of(threshold(m, "cex-perp"))
    assert crit.limit == Decimal(5)
    assert "лимит группы" in crit.detail


def test_flat_benchmark_gives_nothing():
    """Бенчмарк без просадки → доля от нуля есть ноль; планка откатывается на групповую."""
    flat = candles([100, 101, 102, 103])
    trades = [trade(i, "5") for i in range(35)]
    m = metrics(
        trades, flat, capital=Decimal(1000), window=WINDOW,
        extra={"allocation": True, "benchmark_kind": "btc_dca"},
    )
    assert m.benchmark_max_dd_pct == Decimal(0)
    assert dd_of(threshold(m, "cex-spot")).limit == Decimal(5)


def test_allocation_still_fails_when_worse_than_half():
    """Правило, просевшее почти как сам актив, планку не проходит — в этом и смысл."""
    trades = [trade(0, "-400")] + [trade(i, "30") for i in range(1, 35)]
    m, result = sample(trades, DEEP, allocation=True)
    crit = dd_of(result)
    assert m.max_dd_pct > Decimal(25)
    assert not crit.passed
