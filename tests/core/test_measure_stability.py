"""Устойчивость: порог смотрит на много окон, а не на одно (В12, замечание 08.09.2026).

Одиночное окно даёт вердикт-свойство нарезки: у `pifagor-forever-sma` он менялся
с «преимущество +6.8 пункта» на «разгром −2.8 млн» от сдвига границы окна.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Branch
from lab.core.measure import threshold
from lab.core.measure.types import Stability
from tests.core.test_ladder import make_metrics

WINDOW = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))


def _with(stability: Stability | None):
    """Метрики, проходящие остальные критерии; отличается только устойчивость."""
    return make_metrics().model_copy(update={"stability": stability})


def test_without_stability_threshold_works_as_before():
    """Оценки нет — порог судит по одному окну и молчит про устойчивость."""
    result = threshold(_with(None), Branch.CEX_SPOT)

    assert result.status == "passed"
    assert not [c for c in result.criteria if c.name.startswith("stability")]


def test_unstable_strategy_fails_even_with_good_single_window():
    """Пять окон из двадцати шести — это не преимущество, сколь бы хорош ни был замер."""
    result = threshold(
        _with(Stability(windows=26, profitable=8, ahead=5, compared=26)), Branch.CEX_SPOT
    )

    assert result.status == "failed"
    assert result.failed_names() == ["stability_profitable", "stability_vs_benchmark"]


def test_stable_strategy_passes():
    result = threshold(
        _with(Stability(windows=20, profitable=15, ahead=13, compared=20)), Branch.CEX_SPOT
    )

    assert result.status == "passed"
    names = {c.name for c in result.criteria}
    assert {"stability_profitable", "stability_vs_benchmark"} <= names


def test_too_few_windows_means_criteria_are_not_applied():
    """Три окна — не статистика; критерии не добавляются, а не считаются пройденными."""
    result = threshold(
        _with(Stability(windows=3, profitable=1, ahead=0, compared=3)), Branch.CEX_SPOT
    )

    assert result.status == "passed"
    assert not [c for c in result.criteria if c.name.startswith("stability")]


def test_no_benchmark_in_any_window_is_not_a_failure():
    """Бенчмарка не было ни в одном окне — сравнивать не с чем, это нехватка данных."""
    result = threshold(
        _with(Stability(windows=10, profitable=8, ahead=0, compared=0)), Branch.CEX_SPOT
    )

    assert result.status == "insufficient"
    by_name = {c.name: c for c in result.criteria}
    assert by_name["stability_vs_benchmark"].passed is None


@pytest.mark.parametrize(
    ("profitable", "windows", "expected"),
    [(15, 20, Decimal(75)), (0, 10, Decimal(0)), (0, 0, Decimal(0))],
)
def test_shares_are_percentages(profitable, windows, expected):
    st = Stability(windows=windows, profitable=profitable, ahead=0, compared=0)
    assert st.profitable_pct == expected
    assert st.ahead_pct is None
