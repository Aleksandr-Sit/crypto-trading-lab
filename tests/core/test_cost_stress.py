from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.config import load_threshold
from lab.config.models import CostStressConfig
from lab.contracts import Costs
from lab.core.measure import ClosedTrade, metrics, threshold
from lab.core.measure.stress import cost_stress
from lab.core.measure.types import Stability

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def trade(pnl="5", **costs):
    return ClosedTrade(instrument="SYN/USD", side="long", qty=Decimal(1),
                       entry_price=Decimal(100), exit_price=Decimal(105),
                       opened_at=NOW, closed_at=NOW + timedelta(hours=1),
                       pnl_gross=Decimal(pnl), costs=Costs(**costs))


def test_signed_components_stressed_before_netting():
    t = trade(fee=Decimal(2), funding=Decimal(-4), slippage=Decimal(-1),
              gas=Decimal(1), priority_fee=Decimal(1), royalty=Decimal(1))
    s = cost_stress([t], CostStressConfig())
    # Baseline costs = 0; stressed = 10 - 2.5 + 205*5/10000 = 7.6025.
    assert s.closed_net_pnl == Decimal("-2.6025")
    assert s.additional_cost == Decimal("7.6025")
    assert s.ev_per_trade == s.closed_net_pnl < t.pnl_net


def test_no_trades_has_no_stressed_ev():
    assert cost_stress([], CostStressConfig()).ev_per_trade is None


def measured(pnl="5"):
    return metrics([trade(pnl, fee=Decimal(1)) for _ in range(30)], Decimal(-1),
                   capital=Decimal(10000), window=(NOW, NOW + timedelta(days=30)),
                   extra={"stability": Stability(windows=10, profitable=10, compared=10, ahead=10),
                          "unrealized_pnl": Decimal(99999)})


def criterion(m, rung="semi", config=None):
    return next(c for c in threshold(m, "cex-spot", rung=rung, config=config).criteria
                if c.name == "cost_stress_ev")


def test_closed_ev_ignores_open_profit():
    m = measured("1.5")
    assert m.ev_per_trade > 0
    assert m.cost_stress.ev_per_trade == Decimal("-0.6025")
    assert criterion(m).passed is False
    assert criterion(m, "auto").passed is False


def test_extra_cannot_override_calculated_stress():
    m = metrics([trade()], None, capital=Decimal(1000),
                window=(NOW, NOW + timedelta(days=1)), extra={"cost_stress": None})
    assert m.cost_stress is not None


def test_positive_stress_and_legacy_or_stale_scenario():
    m = measured()
    assert criterion(m).passed is True
    assert criterion(m.model_copy(update={"cost_stress": None})).passed is None
    cfg = load_threshold()
    changed = cfg.model_copy(update={"cost_stress": CostStressConfig(multiplier=Decimal(3))})
    assert criterion(m, config=changed).passed is None
    wrong_count = m.cost_stress.model_copy(update={"n_trades": 1})
    assert criterion(m.model_copy(update={"cost_stress": wrong_count})).passed is None


@pytest.mark.parametrize("kwargs", [{"multiplier": 1}, {"extra_turnover_bps": 0}])
def test_stress_cannot_be_disabled_by_configuration(kwargs):
    with pytest.raises(ValueError):
        CostStressConfig(**kwargs)
