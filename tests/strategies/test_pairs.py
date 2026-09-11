"""Парный статистический арбитраж (11.09.2026).

Первая стратегия относительной стоимости: доход из РАСХОЖДЕНИЯ двух активов, а не из
направления рынка и не из премии за плечо. Проверяются места, где такое правило ломается
тихо: нормировка рядов разного масштаба, отбор без заглядывания вперёд, направление ног
и обязательный выход при разрыве связи.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies import registry as code_registry
from lab.strategies.pairs import normalized, spread_stats

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SID = "cex-perp-paper-pairs-cointegration"
A, B, C = "AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"


def _bar(instrument: str, i: int, price: Decimal, volume=Decimal(1_000_000)) -> Candle:
    return Candle(
        instrument=instrument,
        tf="1d",
        ts=T0 + DAY * i,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
    )


def _strategy(**params):
    base = {
        "formation_days": 20,
        "trading_days": 40,
        "entry_sigma": 2.0,
        "exit_sigma": 0.5,
        "stop_sigma": 4.0,
        "top_pairs": 2,
        "max_hold_days": 30,
        "min_daily_volume_usd": 0,
        "capital_usd": 10_000,
        "max_notional_pct_of_branch": 60,
    }
    s = code_registry.build(SID, params={**base, **params})
    s.manifest = s.manifest.model_copy(update={"instruments": [A, B, C]})
    return s


def _feed_day(s, i: int, prices: dict[str, Decimal]):
    out = []
    for name, price in prices.items():
        out += s.on_bar(_bar(name, i, price))
    return out


def test_normalization_makes_different_scales_comparable():
    """BTC по 60 000 и DOGE по 0.2 нельзя вычитать: спред был бы про масштаб,
    а не про расхождение."""
    big = normalized([Decimal(60_000), Decimal(66_000)])
    small = normalized([Decimal("0.2"), Decimal("0.22")])

    assert big == small == [Decimal(1), Decimal("1.1")]
    assert normalized([]) == []
    assert normalized([Decimal(0), Decimal(1)]) == [], "нулевая база — нормировать не на что"


def test_spread_stats_measure_closeness():
    together = [Decimal(1), Decimal("1.1"), Decimal("1.2")]
    apart = [Decimal(1), Decimal("1.5"), Decimal(2)]

    _, _, close_sq = spread_stats(together, together)
    _, sigma, far_sq = spread_stats(together, apart)

    assert close_sq == 0, "ряд сам с собой — расстояние ноль"
    assert far_sq > 0 and sigma > 0


def test_pairs_are_formed_only_from_past_data():
    """Отбор идёт на окне формирования: пара, сошедшаяся ПОЗЖЕ, выбрана быть не могла."""
    s = _strategy(formation_days=20, trading_days=40)
    for i in range(21):  # A и B ходят вместе, C живёт своей жизнью
        _feed_day(s, i, {A: Decimal(100 + i), B: Decimal(50) + Decimal(i) / 2, C: Decimal(30)})

    assert s.formed, "после окна формирования пары должны быть отобраны"
    chosen = {(p.left, p.right) for p in s.pairs}
    assert (A, B) in chosen


def test_divergence_opens_both_legs_in_opposite_directions():
    s = _strategy(entry_sigma=1.0)
    for i in range(21):
        _feed_day(s, i, {A: Decimal(100), B: Decimal(100), C: Decimal(30)})
    # A убегает вверх — значит шорт A и лонг B
    signals = _feed_day(s, 21, {A: Decimal(130), B: Decimal(100), C: Decimal(30)})

    by = {sig.instrument: sig.side for sig in signals if sig.instrument in (A, B)}
    assert by == {A: "sell", B: "buy"}, f"ноги разъехались: {by}"


def test_convergence_closes_the_pair():
    s = _strategy(entry_sigma=1.0, exit_sigma=0.5)
    for i in range(21):
        _feed_day(s, i, {A: Decimal(100), B: Decimal(100), C: Decimal(30)})
    _feed_day(s, 21, {A: Decimal(130), B: Decimal(100), C: Decimal(30)})
    assert any(p.qty_left > 0 for p in s.pairs)

    closing = _feed_day(s, 22, {A: Decimal(100), B: Decimal(100), C: Decimal(30)})

    assert {sig.meta.get("reason") for sig in closing} == {"схождение"}
    assert all(p.qty_left == 0 for p in s.pairs)


def test_widening_divergence_cuts_the_pair():
    """Расхождение, которое РАСТЁТ, чаще значит, что один актив умирает,
    а не что схождение близко."""
    s = _strategy(entry_sigma=1.0, stop_sigma=2.0)
    for i in range(21):
        _feed_day(s, i, {A: Decimal(100), B: Decimal(100), C: Decimal(30)})
    _feed_day(s, 21, {A: Decimal(130), B: Decimal(100), C: Decimal(30)})

    closing = _feed_day(s, 22, {A: Decimal(300), B: Decimal(100), C: Decimal(30)})

    assert {sig.meta.get("reason") for sig in closing} == {"связь порвалась"}


def test_one_instrument_is_used_in_one_pair_only():
    """Иначе капитал утроится на одном ряду, и «нейтральность» станет концентрированной ставкой."""
    s = _strategy(top_pairs=3)
    for i in range(21):
        _feed_day(s, i, {A: Decimal(100), B: Decimal(100), C: Decimal(100)})

    used = [name for p in s.pairs for name in (p.left, p.right)]
    assert len(used) == len(set(used)), f"инструмент попал в две пары: {used}"
