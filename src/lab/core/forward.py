"""Frozen observation contracts and conservative, independently recorded forward evidence."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.core.registry import Strategy, _canonical, _fingerprint
from lab.db.base import utcnow
from lab.db.models import (
    FillRow,
    ForwardObservationRow,
    OrderRow,
    SignalRow,
    StrategyRow,
    SystemFlagRow,
)

# Fixed before observation. Late records remain in the journal but cannot prove admission.
MAX_SIGNAL_LAG_S = 60
MAX_FILL_LAG_S = 300


def virtual_health(session: Session, strategy_id: str) -> dict:
    """Current collector health; absence preserves legacy journal operation."""
    row = session.get(SystemFlagRow, f"virtual_forward:{strategy_id}")
    if row is None:
        return {}
    state = dict(row.value or {})
    problems = {k: state[k] for k in (
        "error", "blocked", "archive_pending", "archive_mismatches", "unpriced_positions",
        "open_positions", "execution_errors", "archive_missing",
    ) if state.get(k)}
    checked = state.get("checked_at")
    age = utcnow() - datetime.fromisoformat(checked) if checked else None
    if age is None or age < timedelta(0) or age > timedelta(minutes=3):
        problems["collector_stale"] = True
    return problems


def source_hash(root: Path) -> str:
    """Actual source bytes, independent of checkout path and Windows line endings."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def rules_spec(record: Strategy) -> dict:
    """Conservative: any lab source change starts a new observation."""
    from lab.core.costs import CostModel
    from lab.strategies import registry as code_registry

    try:
        defaults = code_registry.manifest(str(record.params.get("code_id") or record.id)).params
    except code_registry.UnknownStrategy:
        defaults = {}
    return _canonical({
        "source_hash": source_hash(Path(__file__).resolve().parents[1]),
        "branch": record.branch, "venue": record.venue,
        "source_kind": record.source_kind, "source_ref": record.source_ref,
        "instruments": sorted(record.instruments), "timeframe": record.timeframe,
        "params": {**defaults, **record.params}, "stop": record.stop.model_dump(mode="json"),
        "measurement_capital": str(record.params.get("capital", 10_000)),
        "max_signal_lag_s": MAX_SIGNAL_LAG_S, "max_fill_lag_s": MAX_FILL_LAG_S,
        "virtual_costs_version": CostModel().version,
    })


def current_observation(
    session: Session, strategy_id: str, *, start: bool = False,
    at: datetime | None = None,
) -> ForwardObservationRow | None:
    # Serialize starts per strategy; never merge observations after changing away and back.
    row = session.scalar(select(StrategyRow).where(StrategyRow.id == strategy_id).with_for_update())
    if row is None:
        raise ValueError(f"стратегия {strategy_id} не найдена")
    spec = rules_spec(Strategy.from_row(row))
    revision = _fingerprint(spec)
    latest = session.scalar(
        select(ForwardObservationRow)
        .where(ForwardObservationRow.strategy_id == strategy_id)
        .order_by(ForwardObservationRow.started_at.desc(), ForwardObservationRow.id.desc()).limit(1)
    )
    if latest is not None and latest.revision == revision:
        return latest
    if not start:
        return None
    latest = ForwardObservationRow(
        id=uuid4().hex, strategy_id=strategy_id, revision=revision,
        spec=spec, started_at=at or utcnow(),
    )
    session.add(latest)
    session.flush()
    return latest


def fill_proves_forward(
    session: Session, fill_id: str | None, observation: ForwardObservationRow,
    *, mode: str, until: datetime,
) -> bool:
    if fill_id is None:
        return False
    row = session.execute(
        select(FillRow, OrderRow, SignalRow)
        .join(OrderRow, FillRow.order_id == OrderRow.id)
        .join(SignalRow, OrderRow.signal_id == SignalRow.id)
        .where(FillRow.id == fill_id)
    ).first()
    if row is None:
        return False
    fill, order, signal = row
    if (signal.strategy_id != observation.strategy_id
            or signal.observation_id != observation.id or order.mode != mode
            or order.venue != observation.spec["venue"]
            or order.instrument != signal.instrument
            or signal.recorded_at is None or fill.recorded_at is None
            or not signal.inputs_hash):
        return False
    signal_lag = min(signal.ttl, observation.spec["max_signal_lag_s"])
    fill_lag = observation.spec["max_fill_lag_s"]
    return (
        observation.started_at <= signal.decided_at <= signal.recorded_at
        <= order.created_at <= fill.ts <= fill.recorded_at <= until
        and signal.recorded_at - signal.decided_at <= timedelta(seconds=signal_lag)
        and fill.ts - signal.decided_at <= timedelta(seconds=signal.ttl)
        and fill.recorded_at - fill.ts <= timedelta(seconds=fill_lag)
    )
