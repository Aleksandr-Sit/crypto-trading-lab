"""Хранение потока ранних токенов (История 71, R17.5).

Правило хранения одно: снимок токена пишется полностью только если токен прошёл первичный
фильтр; по отсеянному остаётся одна строка с причиной и агрегаты окна. Иначе 1000 токенов
в час превращают базу в свалку, а замер — в перебор мусора.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from lab.feeds.dex.honesty import Checklist
from lab.feeds.dex.models import (
    MemeFlowStatsRow,
    MemeTokenRow,
    MemeTokenSnapshotRow,
    MemeTxAttemptRow,
)
from lab.feeds.dex.stream import FlowAggregate, StreamVerdict
from lab.feeds.dex.types import TokenInfo

if TYPE_CHECKING:  # слой фидов не зависит от слоя исполнителей — только тип попытки
    from lab.executors.dex.swap import TxAttempt


def _checklist_json(checklist: Checklist | None) -> dict:
    if checklist is None:
        return {}
    return {
        "passed": checklist.passed,
        "failed": checklist.failed_names(),
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "value": None if c.value is None else str(c.value),
                "limit": None if c.limit is None else str(c.limit),
                "detail": c.detail,
            }
            for c in checklist.checks
        ],
        "checked_at": checklist.checked_at.isoformat(),
    }


def load_token(session, chain: str, address: str) -> MemeTokenRow | None:
    return session.execute(
        select(MemeTokenRow).where(MemeTokenRow.chain == chain, MemeTokenRow.address == address)
    ).scalar_one_or_none()


def save_token(
    session,
    token: TokenInfo,
    verdict: StreamVerdict | None = None,
    *,
    checklist: Checklist | None = None,
    now: datetime | None = None,
) -> MemeTokenRow:
    """Строка токена всегда; снимок в историю — только у прошедшего фильтр."""
    now = now or datetime.now(UTC)
    passed = bool(verdict.passed) if verdict is not None else True
    row = load_token(session, token.chain, token.address)
    if row is None:
        row = MemeTokenRow(
            chain=token.chain,
            address=token.address,
            first_seen_at=now,
        )
        session.add(row)
    row.symbol = token.symbol
    row.name = token.name
    row.source = token.source
    row.venue = token.venue
    row.pair = token.pair
    row.created_at = token.created_at
    row.last_seen_at = now
    row.passed_filter = passed
    row.reason = "" if passed else (verdict.reason if verdict else "")
    row.price_usd = token.price_usd
    row.liquidity_usd = token.liquidity_usd
    row.volume_usd = token.volume_usd
    if checklist is not None:
        row.honesty_json = _checklist_json(checklist)
    row.snapshot_json = token.model_dump(mode="json")
    if passed:
        session.add(
            MemeTokenSnapshotRow(
                chain=token.chain,
                address=token.address,
                ts=now,
                price_usd=token.price_usd,
                liquidity_usd=token.liquidity_usd,
                volume_usd=token.volume_usd,
                buys=token.buys,
                sells=token.sells,
                payload_json=token.model_dump(mode="json"),
            )
        )
    return row


def token_history(session, chain: str, address: str) -> list[MemeTokenSnapshotRow]:
    return list(
        session.execute(
            select(MemeTokenSnapshotRow)
            .where(
                MemeTokenSnapshotRow.chain == chain,
                MemeTokenSnapshotRow.address == address,
            )
            .order_by(MemeTokenSnapshotRow.id)
        ).scalars()
    )


def tracked_tokens(session, *, chain: str | None = None) -> list[MemeTokenRow]:
    query = select(MemeTokenRow).where(MemeTokenRow.passed_filter.is_(True))
    if chain is not None:
        query = query.where(MemeTokenRow.chain == chain)
    return list(session.execute(query.order_by(MemeTokenRow.id)).scalars())


def save_flow(session, aggregate: FlowAggregate) -> MemeFlowStatsRow:
    row = MemeFlowStatsRow(
        window_from=aggregate.window_start,
        window_to=aggregate.window_end,
        seen=aggregate.seen,
        passed=aggregate.passed,
        rejected=aggregate.rejected,
        tokens=aggregate.tokens,
        reasons_json=dict(aggregate.by_reason),
        chains_json=dict(aggregate.by_chain),
        sources_json=dict(aggregate.by_source),
    )
    session.add(row)
    return row


def flow_windows(session, *, limit: int = 100) -> list[MemeFlowStatsRow]:
    return list(
        session.execute(
            select(MemeFlowStatsRow).order_by(MemeFlowStatsRow.id).limit(limit)
        ).scalars()
    )


def save_attempts(
    session,
    attempts: Iterable[TxAttempt] | Iterable[Any],
    *,
    strategy_id: str = "",
    venue: str = "",
) -> list[MemeTxAttemptRow]:
    """Попытки транзакций в базу — включая неудачные: причина и стоимость (История 70)."""
    rows = [
        MemeTxAttemptRow(
            strategy_id=strategy_id,
            venue=venue,
            order_id=a.order_id,
            attempt=a.attempt,
            status=a.status,
            tx=a.tx,
            reason=a.reason,
            gas_usd=a.gas_usd,
            priority_fee_usd=a.priority_fee_usd,
            ts=a.at,
        )
        for a in attempts
    ]
    session.add_all(rows)
    return rows


def tx_attempts(
    session, *, order_id: str | None = None, strategy_id: str | None = None
) -> Sequence[MemeTxAttemptRow]:
    query = select(MemeTxAttemptRow)
    if order_id is not None:
        query = query.where(MemeTxAttemptRow.order_id == order_id)
    if strategy_id is not None:
        query = query.where(MemeTxAttemptRow.strategy_id == strategy_id)
    return list(session.execute(query.order_by(MemeTxAttemptRow.attempt)).scalars())


__all__ = [
    "flow_windows",
    "load_token",
    "save_attempts",
    "save_flow",
    "save_token",
    "token_history",
    "tracked_tokens",
    "tx_attempts",
]
