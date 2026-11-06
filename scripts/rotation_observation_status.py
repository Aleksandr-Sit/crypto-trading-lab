"""Read-only post-deployment evidence; run with the worker's Python and DATABASE_URL."""

import json
import os
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from lab.core.forward import rules_spec, virtual_health
from lab.core.registry import Strategy, _fingerprint
from lab.db.models import (
    ForwardObservationRow,
    RungTransitionRow,
    SignalRow,
    StrategyRow,
    SystemFlagRow,
    TradeRow,
    VirtualBarRow,
)

engine = create_engine(os.environ["DATABASE_URL"])
with Session(engine) as session:
    session.execute(text("SET TRANSACTION READ ONLY"))
    strategy_id = "cex-spot-external-rotation-gold-btc"
    record = session.get_one(StrategyRow, strategy_id)
    observations = list(session.scalars(select(ForwardObservationRow).where(
        ForwardObservationRow.strategy_id == strategy_id,
    ).order_by(ForwardObservationRow.started_at)))
    flag = session.get(SystemFlagRow, f"virtual_forward:{strategy_id}")
    ids = [row.id for row in observations]
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "schema_version": session.scalar(text("SELECT version_num FROM alembic_version")),
        "code_version": os.environ.get("LAB_CODE_VERSION"),
        "strategy_id": strategy_id, "rung": record.rung, "status": record.status,
        "current_rules_revision": _fingerprint(rules_spec(Strategy.from_row(record))),
        "observations": [{"id": row.id, "revision": row.revision,
                          "started_at": row.started_at.isoformat(), "spec": row.spec}
                         for row in observations],
        "collector": flag.value if flag else None,
        "health_problems": virtual_health(session, strategy_id),
        "bars_by_archive_status": dict(session.execute(select(
            VirtualBarRow.archive_status, func.count(),
        ).where(VirtualBarRow.observation_id.in_(ids)).group_by(
            VirtualBarRow.archive_status,
        )).all()),
        "signals": session.scalar(select(func.count()).select_from(SignalRow).where(
            SignalRow.strategy_id == strategy_id,
        )),
        "trades": session.scalar(select(func.count()).select_from(TradeRow).where(
            TradeRow.strategy_id == strategy_id,
        )),
        "transitions": [{"from": row.from_rung, "to": row.to_rung,
                         "ts": row.ts.isoformat(), "reason": row.reason, "by": row.by}
                        for row in session.scalars(select(RungTransitionRow).where(
                            RungTransitionRow.strategy_id == strategy_id,
                        ).order_by(RungTransitionRow.id))],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
engine.dispose()
