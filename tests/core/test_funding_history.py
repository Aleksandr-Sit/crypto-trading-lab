"""Симулятор начисляет фандинг по РЕАЛЬНОЙ истории, а не по константе (08.09.2026).

Пока ставка была одним числом из параметров, нейтральные стратегии мерить было
бессмысленно: их результат целиком определялся этим числом. Здесь проверяется, что берётся
ставка каждой конкретной выплаты, а при отсутствии истории поведение остаётся прежним —
и что это видно по счётчикам, а не молча.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Branch, Candle, Signal
from lab.core.measure import PaperEngine

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
INST = "BTC/USDT:USDT"


def _bar(i: int, price: Decimal = Decimal(100)) -> Candle:
    return Candle(
        instrument=INST,
        tf="1h",
        ts=T0 + HOUR * i,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1000),
    )


def _engine(**kw) -> PaperEngine:
    return PaperEngine(venue="binance", instrument=INST, tf="1h", branch=Branch.CEX_PERP, **kw)


def _long(engine: PaperEngine, qty: Decimal = Decimal(1)) -> None:
    """Открыть лонг на первом баре: фандинг начисляется на открытую позицию."""
    engine.on_bar(_bar(0))
    engine.submit(
        Signal(
            strategy_id="x",
            decided_at=T0 + HOUR,
            instrument=INST,
            side="buy",
            size=qty,
            price_ref=Decimal(100),
            inputs_hash="h",
            ttl_s=7200,
        )
    )
    engine.on_bar(_bar(1))


def _funding(engine: PaperEngine) -> Decimal:
    return sum((lot.costs.funding for lot in engine.lots), Decimal(0))


def test_rate_of_each_payout_is_taken_from_history():
    """Ставка 1% на восьмичасовой границе даёт ровно 1% от номинала, а не константу."""
    engine = _engine(funding_rates={T0 + HOUR * 8: Decimal("0.01")})
    _long(engine)
    for i in range(2, 10):
        engine.on_bar(_bar(i))

    assert engine.funding_from_history == 1
    assert _funding(engine) == Decimal("0.01") * Decimal(1) * Decimal(100)


def test_without_history_the_constant_is_used_as_before():
    engine = _engine(funding_rate=Decimal("0.0002"))
    _long(engine)
    for i in range(2, 10):
        engine.on_bar(_bar(i))

    assert engine.funding_from_history == 0
    assert _funding(engine) == Decimal("0.0002") * Decimal(100)


def test_missing_moment_falls_back_and_is_counted():
    """Дырка в истории не выдумывает ставку и не молчит: считаем по константе,
    а сколько раз так вышло — видно в счётчике."""
    engine = _engine(funding_rate=Decimal("0.0002"), funding_rates={})
    _long(engine)
    for i in range(2, 10):
        engine.on_bar(_bar(i))

    assert engine.funding_missed == 1
    assert engine.funding_from_history == 0
    assert _funding(engine) == Decimal("0.0002") * Decimal(100)


def test_short_receives_funding_when_rate_is_positive():
    """Лонг платит, шорт получает — знак не должен потеряться при переходе на историю."""
    engine = _engine(funding_rates={T0 + HOUR * 8: Decimal("0.01")})
    engine.on_bar(_bar(0))
    engine.submit(
        Signal(
            strategy_id="x",
            decided_at=T0 + HOUR,
            instrument=INST,
            side="sell",
            size=Decimal(1),
            price_ref=Decimal(100),
            inputs_hash="h",
            ttl_s=7200,
        )
    )
    for i in range(1, 10):
        engine.on_bar(_bar(i))

    assert _funding(engine) == Decimal("-1")


def test_each_boundary_inside_a_daily_bar_uses_its_own_rate():
    """На дневном баре три границы фандинга — и у каждой своя ставка, а не одна на всех."""
    engine = PaperEngine(
        venue="binance",
        instrument=INST,
        tf="1d",
        branch=Branch.CEX_PERP,
        funding_rates={
            T0 + timedelta(hours=8): Decimal("0.01"),
            T0 + timedelta(hours=16): Decimal("0.02"),
            T0 + timedelta(hours=24): Decimal("0.03"),
        },
    )
    day0 = Candle(
        instrument=INST,
        tf="1d",
        ts=T0,
        open=Decimal(100),
        high=Decimal(100),
        low=Decimal(100),
        close=Decimal(100),
        volume=Decimal(1000),
    )
    engine.on_bar(day0)
    engine.submit(
        Signal(
            strategy_id="x",
            decided_at=T0 + timedelta(days=1),
            instrument=INST,
            side="buy",
            size=Decimal(1),
            price_ref=Decimal(100),
            inputs_hash="h",
            ttl_s=86400 * 3,
        )
    )
    engine.on_bar(day0.model_copy(update={"ts": T0 + timedelta(days=1)}))

    # Позиция открыта на втором дне: в него попадают границы 8, 16 и 24 часа второго дня.
    assert engine.funding_from_history + engine.funding_missed == 3
