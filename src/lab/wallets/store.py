"""Хранение статистики кошелька: таблица `wallets_tracked` (схема таска 01)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.db.models import WalletTrackedRow
from lab.wallets.types import Flag, WalletStats


def save_stats(session: Session, stats: WalletStats, marks: Sequence[Flag] = ()) -> None:
    row = session.get(WalletTrackedRow, (stats.address, stats.chain))
    if row is None:
        row = WalletTrackedRow(address=stats.address, chain=stats.chain)
        session.add(row)
    row.stats_json = stats.model_dump(mode="json")
    row.flags_json = [f.model_dump(mode="json") for f in marks]
    row.last_recalc = stats.computed_at or datetime.now(UTC)


def load_stats(
    session: Session, address: str, chain: str
) -> tuple[WalletStats | None, list[Flag]]:
    row = session.get(WalletTrackedRow, (address, chain))
    if row is None or not row.stats_json:
        return None, []
    return WalletStats.model_validate(row.stats_json), [
        Flag.model_validate(f) for f in (row.flags_json or [])
    ]


def tracked(session: Session, chain: str | None = None) -> list[tuple[str, str]]:
    stmt = select(WalletTrackedRow.address, WalletTrackedRow.chain)
    if chain:
        stmt = stmt.where(WalletTrackedRow.chain == chain)
    return [(a, c) for a, c in session.execute(stmt)]


__all__ = ["load_stats", "save_stats", "tracked"]
