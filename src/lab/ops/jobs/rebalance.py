"""Предложение перелива излишка рисковых веток (G05, G05.1).

Излишек = `current_usd − base_usd` по ветке (числа даёт `core.risk.allocation`). Система
предлагает сумму карточкой; перевод делает пользователь руками, а кнопка «перелить»
фиксирует факт — без подтверждения не двигается ничего, даже внутри площадки.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from lab.db.base import utcnow
from lab.discovery.config import DiscoveryConfig, load_discovery
from lab.ops.jobs.models import RebalanceProposalRow

log = logging.getLogger(__name__)

APPLY = {"apply", "moved", "перелил", "перелить", "confirm"}
SKIP = {"skip", "skipped", "оставить", "cancel"}
COOLDOWN = timedelta(days=7)


@dataclass(frozen=True)
class Proposal:
    id: int
    from_branch: str
    to_branch: str
    amount_usd: Decimal
    base_usd: Decimal
    current_usd: Decimal
    status: str
    detail: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str

    @classmethod
    def from_row(cls, row: RebalanceProposalRow) -> Proposal:
        return cls(
            id=row.id,
            from_branch=row.from_branch,
            to_branch=row.to_branch,
            amount_usd=Decimal(row.amount_usd),
            base_usd=Decimal(row.base_usd),
            current_usd=Decimal(row.current_usd),
            status=row.status,
            detail=row.detail,
            created_at=row.created_at,
            decided_at=row.decided_at,
            decided_by=row.decided_by,
        )

    def payload(self) -> dict[str, Any]:
        """Карточка `bot.send_card("rebalance", ...)` — суммы строками, как в outbox."""
        return {
            "proposal_id": self.id,
            "from_branch": self.from_branch,
            "to_branch": self.to_branch,
            "amount_usd": money(self.amount_usd),
            "detail": self.detail,
        }


def money(value: Any) -> str:
    """Сумма строкой без экспоненты: Decimal('3E+2') в карточке читается как «300»."""
    return format(Decimal(str(value)).normalize(), "f")


class ProposalNotFound(Exception):
    pass


def rebalance_proposal(
    session_scope: Callable[[], Any],
    *,
    risk: Any,
    branches: Sequence[str] | None = None,
    to_branch: str | None = None,
    bot: Any = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
) -> list[Proposal]:
    """Излишек каждой рисковой ветки сверх базы → предложение перелива."""
    cfg = config or load_discovery()
    at = now or utcnow()
    target = to_branch or cfg.rebalance.to_branch
    made: list[Proposal] = []
    with session_scope() as session:
        for branch in branches or cfg.rebalance.risky_branches:
            try:
                allocation = risk.allocation(branch)
            except Exception as err:  # noqa: BLE001 — ветка без раскладки не роняет прогон
                log.warning("Раскладка ветки %s недоступна: %s", branch, err)
                continue
            if getattr(allocation, "stale", False):
                continue  # устаревший баланс площадки (R30i.5) — не предлагаем вслепую
            surplus = Decimal(allocation.current_usd) - Decimal(allocation.base_usd)
            if surplus < cfg.rebalance.min_amount_usd:
                continue
            if _recent(session, branch, at):
                continue
            row = RebalanceProposalRow(
                from_branch=branch,
                to_branch=target,
                amount_usd=surplus,
                base_usd=Decimal(allocation.base_usd),
                current_usd=Decimal(allocation.current_usd),
                status="proposed",
                detail=(
                    f"излишек сверх базы: {money(surplus)} USD "
                    f"({money(allocation.current_usd)} против базы "
                    f"{money(allocation.base_usd)})"
                ),
                created_at=at,
            )
            session.add(row)
            session.flush()
            proposal = Proposal.from_row(row)
            made.append(proposal)
            if bot is not None:
                bot.send_card_sync("rebalance", proposal.payload())
        session.flush()
    return made


def confirm_rebalance(
    session: Any,
    proposal_id: int | str,
    decision: str,
    *,
    by: str = "operator",
    now: datetime | None = None,
) -> Proposal:
    """Кнопка карточки: «перелить» фиксирует факт перевода, «оставить» закрывает предложение."""
    row = session.get(RebalanceProposalRow, int(proposal_id))
    if row is None:
        raise ProposalNotFound(f"предложение перелива #{proposal_id} не найдено")
    action = decision.strip().lower()
    if action in APPLY:
        row.status = "moved"
    elif action in SKIP:
        row.status = "skipped"
    else:
        raise ValueError(f"неизвестное решение по переливу: {decision!r}")
    row.decided_at = now or utcnow()
    row.decided_by = by
    session.flush()
    log.info("Перелив #%s: %s (%s)", row.id, row.status, by)
    return Proposal.from_row(row)


def proposals(session: Any, *, status: str | None = None, branch: str | None = None):
    stmt = select(RebalanceProposalRow).order_by(RebalanceProposalRow.id)
    if status:
        stmt = stmt.where(RebalanceProposalRow.status == status)
    if branch:
        stmt = stmt.where(RebalanceProposalRow.from_branch == branch)
    return [Proposal.from_row(r) for r in session.scalars(stmt)]


def rebalance_hook(
    session_scope: Callable[[], Any], *, by: str = "operator"
) -> Callable[[str, str], None]:
    """Обработчик кнопок для `bot.TraderBot(on_rebalance=...)` (подключает T14)."""

    def hook(ref_id: str, decision: str) -> None:
        with session_scope() as session:
            confirm_rebalance(session, ref_id, decision, by=by)

    return hook


def _recent(session: Any, branch: str, at: datetime) -> bool:
    """Одно предложение по ветке в неделю: задание и так недельное."""
    row = session.scalar(
        select(RebalanceProposalRow)
        .where(
            RebalanceProposalRow.from_branch == branch,
            RebalanceProposalRow.created_at > at - COOLDOWN,
        )
        .limit(1)
    )
    return row is not None


__all__ = [
    "Proposal",
    "ProposalNotFound",
    "money",
    "confirm_rebalance",
    "proposals",
    "rebalance_hook",
    "rebalance_proposal",
]
