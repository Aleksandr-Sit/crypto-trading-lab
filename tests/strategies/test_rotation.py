"""Ротация золото/BTC: правило, голосование окон и защита от сравнения разных суток."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies.registry import build

STRATEGY_ID = "cex-spot-external-rotation-gold-btc"
RISK = "BTC/USDT"
SAFE = "PAXG/USDT"
START = datetime(2021, 1, 1, tzinfo=UTC)


def strategy(**params):
    s = build(STRATEGY_ID)
    s.manifest.params.update(params)
    s.reset()
    return s


def bar(instrument: str, day: int, close: float) -> Candle:
    price = Decimal(str(close))
    return Candle(
        ts=START + timedelta(days=day),
        instrument=instrument,
        tf="1d",
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def feed(s, days: int, risk, safe, *, skip_safe: set[int] | None = None):
    """Прогнать обе ноги по дням; `skip_safe` — дни, когда защитной ноги нет вовсе."""
    out = []
    for d in range(days):
        out += s.on_bar(bar(RISK, d, risk(d)))
        if not (skip_safe and d in skip_safe):
            out += s.on_bar(bar(SAFE, d, safe(d)))
    return out


def test_registered_and_has_both_legs():
    s = build(STRATEGY_ID)
    assert set(s.manifest.instruments) == {RISK, SAFE}
    assert s.manifest.venue == "binance"


def test_buys_the_leading_asset():
    """BTC растёт, золото стоит — правило обязано оказаться в BTC."""
    s = strategy()
    signals = feed(s, 120, lambda d: 100 + d, lambda _: 100)
    buys = [x for x in signals if x.side == "buy"]
    assert buys, "ни одной покупки"
    assert all(x.instrument == RISK for x in buys)
    assert s.want == RISK


def test_rotates_into_gold_when_btc_breaks():
    """BTC рос, потом рухнул ниже золота — правило обязано выйти и перейти в золото."""
    s = strategy()
    signals = feed(
        s,
        260,
        lambda d: 100 + d if d < 120 else max(5.0, 220 - (d - 120) * 2.5),
        lambda d: 100 + d * 0.5,
    )
    kinds = [(x.instrument, x.side) for x in signals]
    assert (RISK, "buy") in kinds
    assert (RISK, "sell") in kinds
    assert (SAFE, "buy") in kinds
    assert s.want == SAFE


def test_goes_to_cash_when_both_fall():
    """Оба падают — держать нечего, это единственный случай кэша."""
    s = strategy()
    feed(s, 200, lambda d: max(5.0, 200 - d), lambda d: max(5.0, 200 - d * 0.8))
    assert s.want == "CASH"
    assert all(leg.qty == 0 for leg in s.legs.values())


def test_decision_waits_for_both_legs_on_the_same_day():
    """Главная ловушка проекта: цены разных суток сравнивать нельзя.

    Пока защитная нога не пришла в ТОТ ЖЕ день, решение не пересматривается —
    иначе «относительный импульс» стал бы движением рынка за сутки. Проверяем
    на отставшей ноге: история у неё есть, нет только сегодняшнего бара.
    """
    s = strategy()
    feed(s, 120, lambda d: 100 + d, lambda _: 100)
    settled = s.decided_on
    assert settled is not None

    # Дальше приходит только рисковая нога — защитная отстала. Решение замирает.
    for d in range(120, 150):
        s.on_bar(bar(RISK, d, 100 + d))
    assert s.decided_on == settled, "решение пересмотрено по одной ноге"

    # Как только обе ноги снова на одной дате — решение может обновиться.
    s.on_bar(bar(SAFE, 150, Decimal(100)))
    s.on_bar(bar(RISK, 150, Decimal(250)))
    assert s.decided_on > settled


def test_no_decision_without_history_on_the_second_leg():
    """Одного бара защитной ноги мало: импульс не с чем сравнить, и правило молчит."""
    s = strategy()
    for d in range(120):
        s.on_bar(bar(RISK, d, 100 + d))
    s.on_bar(bar(SAFE, 119, Decimal(100)))
    assert s.decided_on is None
    assert s.want == "CASH"


def test_gap_in_one_leg_does_not_freeze_the_rule():
    """Разрыв в ряду золота не должен останавливать правило навсегда."""
    s = strategy()
    feed(s, 200, lambda d: 100 + d, lambda _: 100, skip_safe=set(range(150, 160)))
    assert s.want == RISK


def test_rebalance_respects_the_period():
    """Решение пересматривается раз в неделю, а не каждый бар."""
    s = strategy(rebalance_days=7)
    feed(s, 120, lambda d: 100 + d, lambda _: 100)
    first = s.decided_on
    s.on_bar(bar(RISK, 120, Decimal(300)))
    s.on_bar(bar(SAFE, 120, Decimal(100)))
    assert (s.decided_on - first).days >= 7 or s.decided_on == first


def test_windows_vote_by_majority():
    """Голосование: окно без истории не голосует вовсе, а не голосует за кэш."""
    s = strategy(windows_weeks=[4, 8, 12])
    feed(s, 40, lambda d: 100 + d, lambda _: 100)
    # На сороковом дне 12-недельного окна ещё нет — голосов меньше трёх, но решение есть.
    assert sum(s.votes.values()) < 3
    assert s.want == RISK


def test_position_is_never_doubled():
    """Повторные бары в том же решении не должны докупать."""
    s = strategy()
    signals = feed(s, 200, lambda d: 100 + d, lambda _: 100)
    buys = [x for x in signals if x.instrument == RISK and x.side == "buy"]
    sells = [x for x in signals if x.instrument == RISK and x.side == "sell"]
    assert len(buys) - len(sells) == 1, "должна быть ровно одна открытая позиция"


def test_reason_reaches_meta():
    """Причина выхода обязана попасть в снимок: без неё поведение не разобрать."""
    s = strategy()
    signals = feed(
        s,
        260,
        lambda d: 100 + d if d < 120 else max(5.0, 220 - (d - 120) * 2.5),
        lambda d: 100 + d * 0.5,
    )
    sells = [x for x in signals if x.side == "sell"]
    assert sells and sells[0].meta.get("reason")
    assert sells[0].meta.get("kind") == "rotate_out"
