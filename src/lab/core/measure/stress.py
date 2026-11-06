"""Sensitivity on fixed closed trades, not a replay of execution or positions."""

from collections.abc import Sequence
from decimal import Decimal

from lab.config.models import CostStressConfig
from lab.core.measure.types import ClosedTrade, CostStress

METHOD = "fixed-trades-component-stress/v1"


def cost_stress(trades: Sequence[ClosedTrade], config: CostStressConfig) -> CostStress:
    additional = Decimal(0)
    net = Decimal(0)
    for trade in trades:
        # Stress each signed component separately: netting funding receipts against
        # fees first would hide fragility and could increase profit under stress.
        baseline = trade.costs.total
        stressed = sum((value * config.multiplier if value >= 0 else value / config.multiplier
                        for value in trade.costs.model_dump().values()), Decimal(0))
        stressed += trade.turnover * config.extra_turnover_bps / 10_000
        additional += stressed - baseline
        net += trade.pnl_gross - stressed
    return CostStress(method=METHOD, multiplier=config.multiplier,
                      extra_turnover_bps=config.extra_turnover_bps, n_trades=len(trades),
                      closed_net_pnl=net, ev_per_trade=net / len(trades) if trades else None,
                      additional_cost=additional)
