"""Фандинг-арбитраж: спот-лонг плюс шорт перпа (08.09.2026).

Первая стратегия, не зависящая от направления рынка, — ради таких делались портфельный
замер (две ноги одновременно) и история ставок фандинга (весь доход именно в ней).
Проверяются правила, а не доходность: решение по ставке, обе ноги одним баром, выдержка
перед выходом, выход по базису.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event
from lab.strategies import registry as code_registry
from lab.strategies.neutral import annualized_pct, base_of, is_perp

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SID = "cex-perp-api-funding-arb-spot-hedge"
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"


def _bar(instrument: str, i: int, price: Decimal) -> Candle:
    return Candle(
        instrument=instrument,
        tf="1h",
        ts=T0 + HOUR * i,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1000),
    )


def _funding(rate: str, i: int = 8) -> Event:
    return Event(
        kind="funding",
        ts=T0 + HOUR * i,
        payload={"instrument": PERP, "rate": Decimal(rate)},
    )


def _strategy(**params):
    base = {"capital_usd": 10_000, "max_notional_pct_of_branch": 50}
    strategy = code_registry.build(SID, params={**base, **params})
    # Ограничиваем манифест одной связкой: так проверяются правила, а не деление капитала.
    strategy.manifest = strategy.manifest.model_copy(update={"instruments": [SPOT, PERP]})
    return strategy


def _feed_prices(strategy, i: int, spot: str = "100", perp: str = "100") -> None:
    strategy.on_bar(_bar(SPOT, i, Decimal(spot)))
    strategy.on_bar(_bar(PERP, i, Decimal(perp)))


def test_annualized_from_period_rate():
    """0.037% за 8 часов — это 41% годовых: три выплаты в сутки."""
    assert round(float(annualized_pct(Decimal("0.00037"))), 1) == 40.5


def test_instrument_helpers():
    assert base_of("BTC/USDT:USDT") == base_of("BTC/USDT") == "BTC"
    assert is_perp("BTC/USDT:USDT")
    assert not is_perp("BTC/USDT")


def test_high_funding_opens_both_legs_at_once():
    """Обе ноги решаются одним баром: разъехавшись во времени, они перестали бы быть хеджем."""
    s = _strategy(entry_funding_annualized_pct=15)
    _feed_prices(s, 0)

    signals = s.on_event(_funding("0.0005"))  # 0.05% × 3 × 365 = 54.75% годовых

    assert len(signals) == 2
    by_instrument = {sig.instrument: sig for sig in signals}
    assert by_instrument[SPOT].side == "buy"
    assert by_instrument[PERP].side == "sell"
    assert by_instrument[SPOT].size == by_instrument[PERP].size, "ноги должны быть равны"
    assert by_instrument[SPOT].size == Decimal(5000) / Decimal(100)


def test_low_funding_does_not_open():
    s = _strategy(entry_funding_annualized_pct=15)
    _feed_prices(s, 0)

    assert s.on_event(_funding("0.00005")) == []  # 5.5% годовых — ниже порога


def test_exit_waits_for_minimum_hold():
    """Ставка упала сразу — выходить рано: комиссии двух ног съедят весь смысл."""
    s = _strategy(min_hold_funding_intervals=3, exit_funding_annualized_pct=3)
    _feed_prices(s, 0)
    s.on_event(_funding("0.0005"))

    assert s.on_event(_funding("0.000001", 16)) == [], "первая выплата — ещё держим"
    assert s.on_event(_funding("0.000001", 24)) == [], "вторая — тоже"
    closing = s.on_event(_funding("0.000001", 32))

    assert len(closing) == 2
    assert {sig.side for sig in closing} == {"sell", "buy"}


def test_negative_basis_closes_the_pair():
    """Перп дешевле спота — фандинг вот-вот развернётся против нас, связку закрываем."""
    s = _strategy(basis_exit_pct=-1)
    _feed_prices(s, 0)
    s.on_event(_funding("0.0005"))

    quiet = s.on_bar(_bar(SPOT, 1, Decimal(100)))
    closing = s.on_bar(_bar(PERP, 1, Decimal("98.5")))  # базис −1.5%

    assert quiet == []
    assert len(closing) == 2, "закрываются обе ноги, а не одна"


def test_basis_is_not_measured_across_different_bars():
    """Цена спота свежая, цена перпа — с прошлого часа: их разность не базис, а движение рынка.

    Первый замер закрыл связку 156 раз из 156 «по базису», хотя настоящий базис BTC не
    отходил дальше −0.04% от −1% порога: сравнивались цены РАЗНЫХ моментов, и на падающем
    часе разность легко уходила за порог. Здесь цена падает на 3% за час — выхода быть не должно.
    """
    s = _strategy(basis_exit_pct=-1)
    _feed_prices(s, 0)
    s.on_event(_funding("0.0005"))

    # Бар спота нового часа: у перпа в состоянии ещё цена предыдущего.
    assert s.on_bar(_bar(SPOT, 1, Decimal(103))) == [], "разность с прошлым часом — не базис"
    # А когда подошёл перп того же часа, базис нулевой — и держим дальше.
    assert s.on_bar(_bar(PERP, 1, Decimal(103))) == []


def test_no_second_leg_means_no_trade():
    """Пока в потоке только перп, открывать нечем: хедж без спота — это просто шорт."""
    s = _strategy()
    s.on_bar(_bar(PERP, 0, Decimal(100)))

    assert s.on_event(_funding("0.0005")) == []


def test_other_events_are_ignored():
    s = _strategy()
    _feed_prices(s, 0)
    assert s.on_event(Event(kind="ticker", ts=T0, payload={"instrument": PERP})) == []
