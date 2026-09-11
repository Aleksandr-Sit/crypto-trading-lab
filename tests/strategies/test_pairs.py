"""Парный статистический арбитраж (11.09.2026).

Первая стратегия относительной стоимости: доход из РАСХОЖДЕНИЯ двух активов, а не из
направления рынка и не из премии за плечо. Проверяются места, где такое правило ломается
тихо: нормировка рядов разного масштаба, отбор без заглядывания вперёд, направление ног
и обязательный выход при разрыве связи.

Про устройство проверок. Решение принимается на ПЕРВОМ баре нового дня по ценам дня
предыдущего — иначе срез рынка неполон. Поэтому сигналы, вызванные ценами дня N,
возвращает вызов `_feed_day(..., N + 1)`.
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
FORM = 20  # дней формирования во всех проверках


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
        "formation_days": FORM,
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


def _formation(s) -> None:
    """Двадцать дней, на которых A и B ходят вместе, но спред живой.

    A колеблется 100/102, B стоит на 100: нормированный спред принимает значения 0 и 0.02,
    среднее 0.01. Ровно синхронные ряды не годятся — у них изменчивость спреда ноль,
    и такая пара не разойдётся никогда (отдельная проверка ниже).
    """
    for i in range(FORM):
        a = Decimal(100) if i % 2 == 0 else Decimal(102)
        _feed_day(s, i, {A: a, B: Decimal(100), C: Decimal(30) + Decimal(i)})


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


def test_perfectly_synchronous_pair_is_rejected():
    """Пара с нулевой изменчивостью спреда не разойдётся никогда — торговать в ней нечего.

    Это не крайний случай из головы: так выглядят два тикера одного актива и любые ряды,
    построенные один из другого. Без проверки они получили бы нулевое расстояние, заняли
    бы все места в отборе и стратегия простояла бы весь период торговли.
    """
    s = _strategy()
    for i in range(FORM + 1):
        # B ровно вдвое дешевле A на каждом баре — нормированные ряды совпадают
        _feed_day(s, i, {A: Decimal(100 + i), B: Decimal(50) + Decimal(i) / 2, C: Decimal(30)})

    assert all({p.left, p.right} != {A, B} for p in s.pairs)


def test_pairs_are_formed_from_past_data_only():
    s = _strategy()
    _formation(s)
    _feed_day(s, FORM, {A: Decimal(100), B: Decimal(100), C: Decimal(50)})

    assert s.formed, "после окна формирования пары должны быть отобраны"
    assert {A, B} == {s.pairs[0].left, s.pairs[0].right}


def test_divergence_opens_both_legs_in_opposite_directions():
    s = _strategy(entry_sigma=2.0)
    _formation(s)
    _feed_day(s, FORM, {A: Decimal(110), B: Decimal(100), C: Decimal(50)})  # A убежал вверх

    signals = _feed_day(s, FORM + 1, {A: Decimal(110), B: Decimal(100), C: Decimal(50)})

    by = {sig.instrument: sig.side for sig in signals if sig.instrument in (A, B)}
    assert by == {A: "sell", B: "buy"}, f"шортим убежавшего, покупаем отставшего: {by}"


def test_convergence_closes_the_pair():
    s = _strategy(entry_sigma=2.0, exit_sigma=0.5)
    _formation(s)
    _feed_day(s, FORM, {A: Decimal(110), B: Decimal(100), C: Decimal(50)})
    _feed_day(s, FORM + 1, {A: Decimal(101), B: Decimal(100), C: Decimal(50)})
    assert any(p.qty_left > 0 for p in s.pairs), "позиция должна была открыться"

    closing = _feed_day(s, FORM + 2, {A: Decimal(101), B: Decimal(100), C: Decimal(50)})

    assert {sig.meta.get("reason") for sig in closing} == {"схождение"}
    assert all(p.qty_left == 0 for p in s.pairs)


def test_widening_divergence_cuts_the_pair():
    """Расхождение, которое РАСТЁТ, чаще значит, что один актив умирает,
    а не что схождение близко."""
    s = _strategy(entry_sigma=2.0, stop_sigma=20.0)
    _formation(s)
    _feed_day(s, FORM, {A: Decimal(105), B: Decimal(100), C: Decimal(50)})
    _feed_day(s, FORM + 1, {A: Decimal(160), B: Decimal(100), C: Decimal(50)})
    assert any(p.qty_left > 0 for p in s.pairs), "позиция должна была открыться"

    closing = _feed_day(s, FORM + 2, {A: Decimal(160), B: Decimal(100), C: Decimal(50)})

    assert {sig.meta.get("reason") for sig in closing} == {"связь порвалась"}


def test_one_instrument_is_used_in_one_pair_only():
    """Иначе капитал утроится на одном ряду, и нейтральность станет концентрированной ставкой."""
    s = _strategy(top_pairs=3)
    for i in range(FORM + 1):
        a = Decimal(100) if i % 2 == 0 else Decimal(102)
        b = Decimal(100) if i % 3 == 0 else Decimal(101)
        _feed_day(s, i, {A: a, B: b, C: Decimal(100) + Decimal(i % 5)})

    used = [name for p in s.pairs for name in (p.left, p.right)]
    assert len(used) == len(set(used)), f"инструмент попал в две пары: {used}"
