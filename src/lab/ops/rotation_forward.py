"""One-time repair of the audited empty rotation journal; caller owns the transaction."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lab.core.ladder.machine import Ladder, default_threshold_fn
from lab.db.models import ForwardObservationRow, SignalRow, StrategyRow, SystemFlagRow, TradeRow

STRATEGY_ID = "cex-spot-external-rotation-gold-btc"
REASON = "Аудит 06.11.2026: повышения по истории без исполнений; возврат к бумажному наблюдению"
EXPECTED_PARAMS = {
    "card": "cex-spot-rotation-gold-btc", "allocation": True, "capital_usd": 10000,
    "stop_loss_pct": 35, "windows_weeks": [4, 8, 12], "rebalance_days": 7,
    "risk_instrument": "BTC/USDT", "safe_instrument": "PAXG/USDT",
}


def prepare_rotation(session: Session, *, apply: bool = False) -> dict:
    """Fail closed if the audited state changed. Run with worker/bot/web stopped to apply."""
    row = session.scalar(select(StrategyRow).where(
        StrategyRow.id == STRATEGY_ID,
    ).with_for_update())
    if row is None:
        raise ValueError("rotation not found")
    if (row.rung != "semi" or row.status != "measuring" or row.venue != "binance"
            or row.branch != "cex-spot" or row.timeframe != "1d"
            or row.instruments != ["BTC/USDT", "PAXG/USDT"]
            or row.params_json != EXPECTED_PARAMS
            or row.stop_json != {"max_dd_pct": "35", "daily_pct": None,
                                 "max_position_pct": None}):
        raise ValueError("rotation differs from audited frozen contract")
    for model in (SignalRow, TradeRow, ForwardObservationRow):
        if session.scalar(select(func.count()).select_from(model).where(
            model.strategy_id == STRATEGY_ID,
        )):
            raise ValueError(f"rotation has existing {model.__tablename__}; manual review required")
    if session.get(SystemFlagRow, f"virtual_forward:{STRATEGY_ID}") is not None:
        raise ValueError("rotation collector state already exists")
    plan = {"strategy_id": STRATEGY_ID, "path": ["semi", "signal", "micro", "paper"],
            "reason": REASON, "applied": False}
    if apply:
        ladder = Ladder(session, threshold=default_threshold_fn())
        for _ in range(3):
            ladder.demote(STRATEGY_ID, REASON, by="operator", snapshot={
                "audit": "docs/research/rotation-forward-audit-2026-11-06.json",
            })
        # Ladder timestamps the observation contract now; collector warmup has its
        # own later timestamp. No signals or executions are invented.
        plan["applied"] = True
    return plan
