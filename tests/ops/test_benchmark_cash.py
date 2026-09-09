"""Бенчмарк «кэш» — это безрисковая ставка, а не ноль (09.09.2026).

Ноль был враньём в пользу нейтральных стратегий: доллар не лежит мёртвым грузом. Из-за
него кэш-энд-керри с +4.40% годовых формально «обошёл бенчмарк» и получил `passed`, хотя
на деле лишь сравнялся с банковской ставкой — а порог должен отвечать на вопрос
«лучше ли, чем НЕ делать этого», где «не делать» означает деньги в казначейских бумагах.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.ops.measure import _benchmark, risk_free_pct

T0 = datetime(2021, 1, 1, tzinfo=UTC)


def test_year_of_cash_earns_the_annual_rate():
    assert round(float(risk_free_pct((T0, T0 + timedelta(days=365)), Decimal(4))), 6) == 4.0


def test_interest_compounds():
    """Купон реинвестируется: 4% за 5.5 лет — это 24.1%, а не 22.0%.

    Простой процент занижал бы планку в пользу стратегии — та же ошибка, что и ноль,
    только меньше: у кэш-энд-керри она превращала +0.41 пункта преимущества в +0.73.
    """
    got = risk_free_pct((T0, T0 + timedelta(days=365 * 5.5)), Decimal(4))

    assert 24.0 < float(got) < 24.2


def test_empty_window_earns_nothing():
    assert risk_free_pct((T0, T0), Decimal(4)) == 0


def test_cash_benchmark_is_not_zero_anymore():
    """Главное: ветка со сравнением «не делать ничего» получает НЕнулевую планку."""
    window = (T0, T0 + timedelta(days=365 * 5))

    value = _benchmark(None, "binance", "1h", window, "cash", risk_free=Decimal(4))

    assert isinstance(value, Decimal)
    assert value > 19, "пять лет под 4% годовых — это больше 19%, а не ноль"


def test_none_still_means_no_comparison():
    assert _benchmark(None, "binance", "1h", (T0, T0 + timedelta(days=30)), "none") is None
