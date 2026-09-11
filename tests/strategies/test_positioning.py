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


def test_percentile_is_the_mean_rank_in_its_own_window():
    history = [Decimal(x) for x in ("1", "2", "3", "4")]
    assert percentile_rank(history, Decimal("5")) == 100, "выше всей истории"
    assert percentile_rank(history, Decimal("0")) == 0, "ниже всей истории"
    assert percentile_rank(history, Decimal("2")) == 37.5, "три из восьми полуинтервалов"
    assert percentile_rank([], Decimal("1")) == 50, "пустое окно — середина, а не край"


def test_flat_history_gives_the_middle_not_an_extreme():
    """Ровный ряд — это отсутствие сигнала, а не экстремум на каждой выплате.

    Считая «долю не превышающих», одинаковая история давала бы КАЖДОЙ ставке сотый
    перцентиль: у спокойных месяцев и стейблкоиновых пар ставка неделями стоит на одном
    значении, и стратегия открывала бы «экстремум» на самой обычной выплате.
    """
    flat = [Decimal("0.0001")] * 30

    assert percentile_rank(flat, Decimal("0.0001")) == 50


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
    # половина капитала, поделённая между двумя инструментами карточки
    assert signals[0].size == Decimal(2500) / Decimal(50_000)
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


# -- вымывание плеча ---------------------------------------------------------------

FLUSH_SID = "cex-perp-api-oi-flush"


def _flush_strategy(**params):
    base = {"capital_usd": 10_000, "max_notional_pct_of_branch": 50, "hold_days": 5}
    s = code_registry.build(FLUSH_SID, params={**base, **params})
    return s.__class__(s.manifest.model_copy(update={"instruments": [BTC]}))


def _metrics(instrument: str, i: int, oi: str) -> Event:
    return Event(
        kind="positioning",
        ts=T0 + HOUR * 24 * i,
        payload={"instrument": instrument, "open_interest": Decimal(oi)},
    )


def _day(s, i: int, price: str, oi: str):
    """День: сначала бар, потом метрики — тот же порядок, что в замере."""
    out = s.on_bar(_bar(BTC, i * 24, price))
    out += s.on_event(_metrics(BTC, i, oi))
    return out


def test_flush_needs_both_the_interest_and_the_price_to_drop():
    """Сжатие интереса на растущей цене — фиксация прибыли, а не принудительные закрытия."""
    s = _flush_strategy(flush_drop_pct=5, flush_price_drop_pct=3)
    _day(s, 0, "50000", "1000")

    assert _day(s, 1, "52000", "900") == [], "интерес сжался, но цена выросла — не вымывание"
    assert _day(s, 2, "50000", "895") == [], "цена упала, но интерес почти не изменился"


def test_flush_opens_a_long():
    s = _flush_strategy(flush_drop_pct=5, flush_price_drop_pct=3)
    _day(s, 0, "50000", "1000")

    signals = _day(s, 1, "47000", "900")  # интерес −10%, цена −6%

    assert len(signals) == 1 and signals[0].side == "buy"
    assert signals[0].meta.get("kind") == "flush_open"
    assert s.flush[BTC].qty == Decimal(5000) / Decimal(47_000)


def test_position_closes_after_the_holding_period():
    """Гипотеза про КОРОТКИЙ отскок после принудительных продаж, а не про смену тренда."""
    s = _flush_strategy(flush_drop_pct=5, flush_price_drop_pct=3, hold_days=5)
    _day(s, 0, "50000", "1000")
    _day(s, 1, "47000", "900")
    assert s.flush[BTC].qty > 0

    assert _day(s, 3, "48000", "890") == [], "рано"
    closing = _day(s, 6, "49000", "880")

    assert len(closing) == 1 and closing[0].side == "sell"
    assert closing[0].meta.get("reason") == "срок вышел"


def test_no_second_entry_while_in_position():
    """Докупать в вымывание — ловить нож: одна позиция на инструмент."""
    s = _flush_strategy(flush_drop_pct=5, flush_price_drop_pct=3, hold_days=30)
    _day(s, 0, "50000", "1000")
    _day(s, 1, "47000", "900")

    assert _day(s, 2, "42000", "800") == [], "второе вымывание подряд позицию не удваивает"


def test_first_day_cannot_decide():
    """Не с чем сравнивать: ни вчерашнего интереса, ни вчерашней цены."""
    s = _flush_strategy()
    assert _day(s, 0, "50000", "1000") == []
