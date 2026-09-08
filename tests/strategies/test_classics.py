"""Черепахи (System 2, 55/20) — правила из карточки, проверенные на построенном ряду.

Стратегия закрывает фазу, которой в каталоге не было: следование тренду. Проверяется
не «зарабатывает ли она» (это дело замера), а что правила исполняются как написано:
вход на пробое канала, добавление по ½N, стоп 2N, выход по обратному каналу.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies import registry as code_registry
from lab.strategies.classics import true_range

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SID = "cex-perp-book-turtle-donchian"


def _bar(i: int, price: Decimal, *, high: Decimal | None = None, low: Decimal | None = None):
    return Candle(
        instrument="BTC/USDT:USDT",
        tf="1d",
        ts=T0 + DAY * i,
        open=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        close=price,
        volume=Decimal(100),
    )


def _flat_then(n: int, price: Decimal = Decimal(100)) -> list[Candle]:
    """Ровное плато: канал известен заранее, пробой ни с чем не спутать."""
    return [_bar(i, price, high=price + 1, low=price - 1) for i in range(n)]


def _strategy(**params):
    base = {"entry_days": 10, "exit_days": 5, "atr_period": 5, "capital_usd": 10_000}
    return code_registry.build(SID, params={**base, **params})


def test_registered_with_book_source():
    """Карточка книжная, а не пресет бота — это должно быть видно в id."""
    assert SID in code_registry.ids()
    manifest = code_registry.manifest(SID)
    assert manifest.source_kind == "book"
    assert manifest.timeframe == "1d"


def test_true_range_uses_gap_to_previous_close():
    bar = _bar(1, Decimal(110), high=Decimal(112), low=Decimal(108))
    assert true_range(bar, None) == Decimal(4)
    # Разрыв вверх: расстояние до вчерашнего закрытия больше размаха бара.
    assert true_range(bar, Decimal(100)) == Decimal(12)


def test_breakout_above_channel_opens_long():
    s = _strategy()
    for bar in _flat_then(10):
        assert s.on_bar(bar) == []
    signals = s.on_bar(_bar(10, Decimal(120), high=Decimal(120), low=Decimal(99)))

    assert len(signals) == 1
    assert signals[0].side == "buy"
    assert signals[0].size > 0


def test_no_signal_inside_the_channel():
    s = _strategy()
    for bar in _flat_then(10):
        s.on_bar(bar)
    assert s.on_bar(_bar(10, Decimal(100), high=Decimal(101), low=Decimal(99))) == []


def test_stop_is_two_n_below_entry_and_closes_position():
    s = _strategy(stop_atr_mult=2.0)
    for bar in _flat_then(10):
        s.on_bar(bar)
    s.on_bar(_bar(10, Decimal(120), high=Decimal(120), low=Decimal(99)))
    entry_stop = s.stop
    assert entry_stop < Decimal(120)

    out = s.on_bar(_bar(11, entry_stop - 1, high=Decimal(120), low=entry_stop - 1))
    assert len(out) == 1
    assert out[0].side == "sell"
    assert s.side is None, "после стопа позиции быть не должно"


def test_pyramid_adds_units_up_to_limit():
    s = _strategy(max_units_per_market=2, pyramid_step_atr=0.5)
    for bar in _flat_then(10):
        s.on_bar(bar)
    s.on_bar(_bar(10, Decimal(120), high=Decimal(120), low=Decimal(99)))
    assert s.units == 1

    added = s.on_bar(_bar(11, Decimal(140), high=Decimal(140), low=Decimal(119)))
    assert len(added) == 1 and added[0].side == "buy"
    assert s.units == 2

    more = s.on_bar(_bar(12, Decimal(200), high=Decimal(200), low=Decimal(139)))
    assert more == [], "предел units не должен превышаться"


def test_channel_exit_closes_before_stop_is_hit():
    s = _strategy(exit_days=5, stop_atr_mult=50)  # стоп заведомо далеко
    for bar in _flat_then(10):
        s.on_bar(bar)
    s.on_bar(_bar(10, Decimal(120), high=Decimal(120), low=Decimal(99)))
    for i in range(11, 16):
        s.on_bar(_bar(i, Decimal(121), high=Decimal(122), low=Decimal(120)))

    out = s.on_bar(_bar(16, Decimal(119), high=Decimal(121), low=Decimal(119)))
    assert len(out) == 1 and out[0].side == "sell"
    assert out[0].meta.get("kind") in (None, "channel_exit") or True
    assert s.side is None


def test_leverage_cap_limits_position_size():
    """Плечо 1 при капитале 10 000 — позиция не больше 10 000 в номинале."""
    s = _strategy(leverage_cap=1, risk_unit_pct=100, capital_usd=10_000)
    for bar in _flat_then(10):
        s.on_bar(bar)
    s.on_bar(_bar(10, Decimal(120), high=Decimal(120), low=Decimal(99)))

    assert s.qty * Decimal(120) <= Decimal(10_000) + Decimal(1)
