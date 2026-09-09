"""Базисный кэш-энд-керри: спот-лонг плюс шорт квартального фьючерса (09.09.2026).

Вторая нейтральная стратегия и первая, работающая со СРОЧНЫМ контрактом: у него есть дата
расчёта, до неё считается доходность, а после неё ряда просто нет. Проверяются места, где
такое правило ломается тихо: разбор даты из имени, приведение премии к году, выбор контракта
и выход перед расчётом.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies import registry as code_registry
from lab.strategies.neutral import basis_annualized_pct, expiry_of

HOUR = timedelta(hours=1)
SID = "cex-perp-api-basis-cash-carry"
SPOT = "BTC/USDT"
FAR = "BTC/USDT:USDT-260925"  # расчёт 25.09.2026 08:00 UTC
NEAR = "BTC/USDT:USDT-260626"
T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _bar(instrument: str, ts: datetime, price: str) -> Candle:
    p = Decimal(price)
    return Candle(
        instrument=instrument,
        tf="1h",
        ts=ts,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal(1000),
    )


def _strategy(instruments=(SPOT, FAR), **params):
    base = {"capital_usd": 10_000, "max_notional_pct_of_branch": 50}
    s = code_registry.build(SID, params={**base, **params})
    return s.__class__(s.manifest.model_copy(update={"instruments": list(instruments)}))


def _tick(strategy, ts, prices: dict[str, str]):
    """Час рынка: бары всех ног с ОДНИМ временем — иначе базис считать не по чему."""
    out = []
    for name, price in prices.items():
        out += strategy.on_bar(_bar(name, ts, price))
    return out


def test_expiry_is_read_from_the_instrument_name():
    assert expiry_of(FAR) == datetime(2026, 9, 25, 8, tzinfo=UTC)
    assert expiry_of("BTC/USDT:USDT") is None, "бессрочный — не срочный"
    assert expiry_of(SPOT) is None


def test_premium_is_annualized_by_days_left():
    """1% премии за 36.5 суток — это 10% годовых; та же премия за год — 1%."""
    assert basis_annualized_pct(Decimal(100), Decimal(101), Decimal("36.5")) == Decimal(10)
    assert basis_annualized_pct(Decimal(100), Decimal(101), Decimal(365)) == Decimal(1)
    assert basis_annualized_pct(Decimal(100), Decimal(101), Decimal("0.5")) is None


def test_high_premium_opens_both_legs():
    s = _strategy(entry_basis_annualized_pct=10)
    # 116 дней до расчёта, премия 2% → около 6% годовых: мало
    assert _tick(s, T0, {SPOT: "100000", FAR: "102000"}) == []
    # премия 5% → около 15.7% годовых: берём
    signals = _tick(s, T0 + HOUR, {SPOT: "100000", FAR: "105000"})

    assert len(signals) == 2
    by = {sig.instrument: sig for sig in signals}
    assert by[SPOT].side == "buy" and by[FAR].side == "sell"
    assert by[SPOT].size == by[FAR].size == Decimal(5000) / Decimal(100_000)


def test_legs_of_different_hours_are_not_compared():
    """Цена контракта с прошлого часа против свежего спота — это не базис, а движение рынка.

    Та же ошибка уже стоила фандинг-арбитражу 156 ложных выходов, поэтому проверяется
    отдельно: пока обе ноги не пришли одним часом, решения быть не должно.
    """
    s = _strategy(entry_basis_annualized_pct=10)
    s.on_bar(_bar(FAR, T0, "105000"))

    assert s.on_bar(_bar(SPOT, T0 + HOUR, "100000")) == [], "часы ног разошлись — не решаем"


def test_contract_too_close_to_expiry_is_not_opened():
    s = _strategy(entry_basis_annualized_pct=10, min_days_to_expiry=14)
    late = datetime(2026, 9, 20, tzinfo=UTC)  # до расчёта 5 суток

    assert _tick(s, late, {SPOT: "100000", FAR: "105000"}) == []


def test_the_most_profitable_contract_wins():
    """Из двух живых контрактов берётся тот, у кого выше премия В ГОДОВЫХ, а не в процентах."""
    s = _strategy(instruments=(SPOT, NEAR, FAR), entry_basis_annualized_pct=10)
    # NEAR: 25 суток, премия 2% → 29% годовых. FAR: 116 суток, премия 5% → 15.7% годовых.
    signals = _tick(s, T0, {SPOT: "100000", NEAR: "102000", FAR: "105000"})

    assert {sig.instrument for sig in signals} == {SPOT, NEAR}


def test_position_is_closed_before_settlement():
    """Расчёт контракта движок не моделирует — закрываемся сами, иначе позиция зависнет."""
    s = _strategy(entry_basis_annualized_pct=10, close_before_expiry_days=1)
    _tick(s, T0, {SPOT: "100000", FAR: "105000"})
    assert s.carries["BTC"].contract == FAR

    closing = _tick(s, datetime(2026, 9, 24, 12, tzinfo=UTC), {SPOT: "100000", FAR: "100010"})

    assert len(closing) == 2
    assert closing[0].meta.get("reason") == "экспирация"
    assert s.carries["BTC"].contract == ""


def test_backwardation_needs_confirmation_before_exit():
    """Премия ушла в минус — выходим, но не на первой же свече: рынок шумит."""
    s = _strategy(entry_basis_annualized_pct=10, negative_basis_hours=24)
    _tick(s, T0, {SPOT: "100000", FAR: "105000"})

    assert _tick(s, T0 + HOUR, {SPOT: "100000", FAR: "99900"}) == [], "первый час — ждём"
    assert _tick(s, T0 + HOUR * 12, {SPOT: "100000", FAR: "99900"}) == [], "полсуток — ждём"
    closing = _tick(s, T0 + HOUR * 25, {SPOT: "100000", FAR: "99900"})

    assert len(closing) == 2
    assert closing[0].meta.get("reason") == "бэквордация"


def test_premium_collected_closes_the_position_early():
    s = _strategy(entry_basis_annualized_pct=10, exit_basis_annualized_pct=2)
    _tick(s, T0, {SPOT: "100000", FAR: "105000"})

    # премия почти выбрана: 0.2% за 116 суток — это 0.6% годовых
    closing = _tick(s, T0 + HOUR, {SPOT: "100000", FAR: "100200"})

    assert len(closing) == 2
    assert closing[0].meta.get("reason") == "премия выбрана"
