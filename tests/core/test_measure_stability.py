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


# --- какие окна идут в счёт (решение владельца 17.09.2026) ------------------------------

def _measured(status: str = "ok", n_trades: int = 40, pnl: str = "5"):
    from types import SimpleNamespace

    metrics = make_metrics(n_trades=n_trades).model_copy(update={"net_pnl_pct": Decimal(pnl)})
    return SimpleNamespace(status=status, metrics=metrics if status == "ok" else None)


def test_window_without_data_or_with_few_trades_is_not_an_observation():
    from lab.ops.measure import counts_as_window

    assert counts_as_window(_measured(), 30)
    assert not counts_as_window(_measured(status="incomplete"), 30)
    assert not counts_as_window(_measured(n_trades=3), 30)
    assert counts_as_window(_measured(n_trades=30), 30)


def test_run_stability_leaves_empty_and_thin_windows_out_of_the_denominator(monkeypatch):
    """Шорт листингов: окно 2018 года без данных и окна с 3–17 сделками шли в знаменатель.

    Пустое окно считалось неудачным, и молодая стратегия проваливала устойчивость тем,
    что ей нет восьми лет; окно с тремя сделками засчитывалось «в плюс» наравне с сотней."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    import lab.core.registry as registry_mod
    import lab.ops.measure as ops_measure

    sequence = iter(
        [
            _measured(status="incomplete"),  # данных ещё нет
            _measured(n_trades=3, pnl="1"),  # в плюсе, но три сделки
            _measured(n_trades=54, pnl="-2"),
            _measured(n_trades=132, pnl="4"),
            _measured(n_trades=155, pnl="8"),
        ]
    )
    monkeypatch.setattr(ops_measure, "run_measure", lambda *a, **k: next(sequence))
    monkeypatch.setattr(ops_measure, "_btc_cagr", lambda *a, **k: None)

    class _Registry:
        def __init__(self, session):
            pass

        def get(self, sid):
            return SimpleNamespace(venue="binance", timeframe="1d")

    monkeypatch.setattr(registry_mod, "Registry", _Registry)

    @contextmanager
    def scope():
        yield None

    now = datetime(2026, 9, 17, tzinfo=UTC)
    stability, rows = ops_measure.run_stability(
        "cex-perp-test",
        session_scope=scope,
        now=now,
        window_days=730,
        step_days=180,
        store=object(),
        history_days=730 + 4 * 180,
        min_trades=30,
    )

    assert len(rows) == 5
    assert stability.windows == 3
    assert stability.skipped == 2
    assert stability.profitable == 2
