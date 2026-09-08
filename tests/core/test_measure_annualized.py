"""Сравнение с бенчмарком ведётся в ГОДОВЫХ (В12, замечание владельца от 08.09.2026).

Разница процентов за окно зависит от длины окна и на длинной истории теряет смысл:
BTC с 2011 года дал +2 855 000%, и вычитание съедает любой результат стратегии. Хуже того,
такой рост был режимом ранней капитализации и в нынешнем триллионном активе не повторится —
значит требовать его побить нельзя ни от какой стратегии.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.core.measure.metrics import annualized_pct

YEAR = timedelta(days=365.25)
START = datetime(2011, 11, 26, tzinfo=UTC)


def _window(years: float) -> tuple[datetime, datetime]:
    return START, START + YEAR * years


def test_annualized_is_readable_where_window_percent_is_not():
    """+2198% за 14.8 года — это ~24% годовых; +2 855 149% за то же окно — ~100%."""
    window = _window(14.79)
    strategy = annualized_pct(Decimal("2197.65"), window)
    btc = annualized_pct(Decimal("2855149"), window)

    assert strategy is not None and btc is not None
    assert 20 < float(strategy) < 28, f"ожидались ~24% годовых, вышло {strategy}"
    assert 90 < float(btc) < 110, f"ожидались ~100% годовых, вышло {btc}"
    # Вывод тот же — стратегия проигрывает, — но числа сопоставимы между собой.
    assert float(strategy) < float(btc)


def test_same_yearly_result_gives_same_annualized_on_different_windows():
    """Удвоение за год и учетверение за два года — одна и та же годовая доходность."""
    one = annualized_pct(Decimal(100), _window(1))
    two = annualized_pct(Decimal(300), _window(2))

    assert one is not None and two is not None
    assert abs(float(one) - float(two)) < 0.5


def test_total_loss_has_no_annualized_value():
    """Капитал ушёл в ноль и ниже — среднегодовой доходности не существует, выдумывать нельзя."""
    assert annualized_pct(Decimal(-100), _window(3)) is None
    assert annualized_pct(Decimal(-150), _window(3)) is None


def test_window_shorter_than_day_is_not_annualized():
    short = (START, START + timedelta(hours=6))
    assert annualized_pct(Decimal(5), short) is None


def test_no_benchmark_means_no_comparison():
    assert annualized_pct(None, _window(2)) is None
