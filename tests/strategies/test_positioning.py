"""Экстремальный фандинг как контр-сигнал (11.09.2026).

Первая стратегия по ПОЗИЦИОНИРОВАНИЮ — данным другой природы, чем цена. Проверяются
правила, а не доходность: перцентиль в своём окне, вход на экстремуме, три выхода,
состояние по инструментам.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event
from lab.strategies import registry as code_registry
from lab.strategies.positioning import percentile_rank

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SID = "cex-perp-api-funding-extreme-reversal"
BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


def _bar(instrument: str, i: int, price: str = "50000") -> Candle:
    p = Decimal(price)
    return Candle(
        instrument=instrument,
        tf="1h",
        ts=T0 + HOUR * i,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal(1000),
    )


def _funding(instrument: str, i: int, rate: str) -> Event:
    return Event(
        kind="funding",
        ts=T0 + HOUR * i,
        payload={"instrument": instrument, "rate": Decimal(rate)},
    )


def _strategy(**params):
    base = {"capital_usd": 10_000, "max_notional_pct_of_branch": 50, "min_history": 10}
    s = code_registry.build(SID, params={**base, **params})
    s.manifest = s.manifest.model_copy(update={"instruments": [BTC, ETH]})
    return s


def _warm(s, instrument: str, n: int = 20, rate: str = "0.0001") -> None:
    """Обычная история: n выплат по одинаковой ставке — перцентиль любой большей будет 100."""
    s.on_bar(_bar(instrument, 0))
    for i in range(n):
        s.on_event(_funding(instrument, i, rate))


def test_percentile_is_the_share_of_history_not_above():
    history = [Decimal(x) for x in ("1", "2", "3", "4")]
    assert percentile_rank(history, Decimal("4")) == 100
    assert percentile_rank(history, Decimal("2")) == 50
    assert percentile_rank(history, Decimal("0")) == 0
    assert percentile_rank([], Decimal("1")) == 50, "пустое окно — середина, а не край"


def test_no_decision_until_history_is_long_enough():
    """Перцентиль по трём выплатам ничего не значит — молчим, пока окно не набралось."""
    s = _strategy(min_history=10)
    s.on_bar(_bar(BTC, 0))
    for i in range(9):
        assert s.on_event(_funding(BTC, i, "0.0001")) == []
    assert s.on_event(_funding(BTC, 9, "0.01")) == [], "десятая — это история, а не сигнал"


def test_extreme_positive_funding_opens_a_short():
    """Ставка выше всего, что было, — лонги переполнены, открываем шорт."""
    s = _strategy(upper_pct=95)
    _warm(s, BTC)

    signals = s.on_event(_funding(BTC, 21, "0.005"))

    assert len(signals) == 1 and signals[0].side == "sell"
    assert signals[0].size == Decimal(2500) / Decimal(50_000), "половина капитала на два инструмента"
    assert s.books[BTC].side == "short"


def test_extreme_negative_funding_opens_a_long():
    s = _strategy(lower_pct=5)
    _warm(s, BTC)

    signals = s.on_event(_funding(BTC, 21, "-0.005"))

    assert len(signals) == 1 and signals[0].side == "buy"
    assert s.books[BTC].side == "long"


def test_ordinary_funding_does_nothing():
    s = _strategy()
    _warm(s, BTC)
    assert s.on_event(_funding(BTC, 21, "0.0001")) == []


def test_exit_when_the_rate_cools_down():
    """Ставка вернулась к середине — переполненности больше нет, держать незачем."""
    s = _strategy(exit_pct=50)
    _warm(s, BTC)
    s.on_event(_funding(BTC, 21, "0.005"))
    assert s.books[BTC].side == "short"

    closing = s.on_event(_funding(BTC, 22, "0.00001"))  # ниже всей истории

    assert len(closing) == 1 and closing[0].side == "buy"
    assert closing[0].meta.get("reason") == "ставка остыла"
    assert s.books[BTC].side is None


def test_exit_by_time_limit():
    """Контр-сигнал не должен превращаться в позицию против тренда: срок вышел — закрываем."""
    s = _strategy(hold_hours=48)
    _warm(s, BTC)
    s.on_event(_funding(BTC, 21, "0.005"))
    opened_at = s.books[BTC].opened_at

    assert s.on_bar(_bar(BTC, 30)) == [], "рано"
    closing = s.on_bar(_bar(BTC, 48))

    assert opened_at is not None
    assert len(closing) == 1 and closing[0].meta.get("reason") == "время вышло"


def test_no_pyramiding_while_the_extreme_deepens():
    """Февраль 2021: ставка выше 100% годовых весь месяц. Докупать против — путь к ликвидации."""
    s = _strategy()
    _warm(s, BTC)
    s.on_event(_funding(BTC, 21, "0.005"))

    assert s.on_event(_funding(BTC, 22, "0.009")) == [], "экстремум углубился — позиция одна"
    assert s.books[BTC].qty == Decimal(2500) / Decimal(50_000)


def test_state_is_per_instrument():
    """Экстремум на BTC не открывает ничего на ETH и не мешает его собственной истории."""
    s = _strategy()
    _warm(s, BTC)
    _warm(s, ETH, rate="0.0003")
    s.on_event(_funding(BTC, 21, "0.005"))

    assert s.books[BTC].side == "short"
    assert s.books[ETH].side is None
    assert s.on_event(_funding(ETH, 21, "0.0003")) == [], "у ETH это обычная ставка"


def test_other_events_are_ignored():
    s = _strategy()
    assert s.on_event(Event(kind="ticker", ts=T0, payload={"instrument": BTC})) == []
