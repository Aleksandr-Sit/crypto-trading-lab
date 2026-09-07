"""Решения оператора по кандидату (R15.1): «в замер» / «отклонить» / «позже».

Кнопка меняет `candidates.decision`; «в замер» заводит стратегию в реестре и
запускает первый режим замера — тот, что назначает `core.measure.measure_plan`
(есть бэктест → `backtest`, форвард-только → `paper`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lab.contracts import CandidateDecision, MeasureMode, StrategyManifest
from lab.core.measure import measure_plan
from lab.core.registry import DuplicateStrategy, Registry
from lab.db.base import utcnow
from lab.db.models import CandidateRow
from lab.discovery.config import DiscoveryConfig, load_discovery

log = logging.getLogger(__name__)

ACCEPT = {"accept", "accepted", "measure", "в замер"}
REJECT = {"reject", "rejected", "отклонить"}
LATER = {"later", "postpone", "позже", "skip"}

# Кошельковые сети смарт-мани-лент → идентификаторы из config/chains.yaml.
CHAIN_ALIASES = {
    "ethereum": "evm",
    "base": "evm",
    "arbitrum": "evm",
    "polygon": "evm",
    "bsc": "bnb",
    "binance": "bnb",
}


class CandidateNotFound(Exception):
    pass


@dataclass(frozen=True)
class DecisionResult:
    candidate_id: int
    decision: CandidateDecision
    strategy_id: str | None = None
    mode: MeasureMode | None = None
    reason: str = ""
    measured: bool = False


def decide(
    session,
    candidate_id: int | str,
    decision: str,
    *,
    registry: Registry | None = None,
    ladder: Any = None,
    measure: Callable[..., Any] | None = None,
    config: DiscoveryConfig | None = None,
    reason: str = "",
    now: datetime | None = None,
) -> DecisionResult:
    """Записать решение по кандидату. Возврат — что именно произошло."""
    row = session.get(CandidateRow, int(candidate_id))
    if row is None:
        raise CandidateNotFound(f"кандидат #{candidate_id} не найден")
    at = now or utcnow()
    action = decision.strip().lower()
    payload = dict(row.payload or {})

    if action in REJECT:
        row.decision = CandidateDecision.REJECTED.value
        row.decided_at = at
        row.payload = {**payload, "reject_reason": reason}
        session.flush()
        return DecisionResult(row.id, CandidateDecision.REJECTED, reason=reason)

    if action in LATER:
        # «Позже» — кандидат остаётся в очереди: решение не принято, счётчик отложек растёт.
        row.payload = {
            **payload,
            "postponed_at": at.isoformat(),
            "postponed": int(payload.get("postponed", 0)) + 1,
        }
        session.flush()
        return DecisionResult(row.id, CandidateDecision.PENDING, reason="отложен")

    if action not in ACCEPT:
        raise ValueError(f"неизвестное решение по кандидату: {decision!r}")

    cfg = config or load_discovery()
    manifest, why = manifest_for(row.kind, row.ref, payload)
    row.decision = CandidateDecision.ACCEPTED.value
    row.decided_at = at
    if manifest is None:
        row.payload = {**payload, "accept_note": why}
        session.flush()
        return DecisionResult(row.id, CandidateDecision.ACCEPTED, reason=why)

    reg = registry or Registry(session)
    try:
        strategy = reg.add(manifest)
        strategy_id = strategy.id
    except DuplicateStrategy as dup:
        strategy_id = dup.existing_id
    plan = measure_plan(manifest)
    mode = MeasureMode.BACKTEST if plan.can_backtest else MeasureMode.PAPER
    row.payload = {**payload, "strategy_id": strategy_id, "measure_mode": mode.value}
    session.flush()

    if ladder is not None:
        ladder.start(strategy_id)
    measured = False
    if measure is not None:
        window = (at - timedelta(days=cfg.remeasure.window_days), at)
        measure(strategy_id=strategy_id, mode=mode.value, window=window)
        measured = True
    return DecisionResult(
        row.id,
        CandidateDecision.ACCEPTED,
        strategy_id=strategy_id,
        mode=mode,
        reason=plan.reason,
        measured=measured,
    )


def manifest_for(
    kind: str, ref: str, payload: Mapping[str, Any]
) -> tuple[StrategyManifest | None, str]:
    """Стратегия по кандидату там, где она выводится однозначно (копитрейд).

    Кандидат-репозиторий или минт кодируется руками — тогда манифеста нет и
    кандидат остаётся принятым с пометкой «нужен ручной манифест»."""
    venue = str(payload.get("venue") or "")
    chain = str(payload.get("chain") or "")
    if kind == "trader" and venue in {"okx", "bybit"}:
        from lab.executors.cex.copy_exchange import make_copy_exchange_strategy

        return make_copy_exchange_strategy(venue, ref).manifest, "копитрейд биржи"
    if venue == "hyperliquid" or chain == "hyperliquid_user":
        from lab.strategies.copy import make_copy_strategy

        return (
            make_copy_strategy("hyperliquid_user", ref, venue="hyperliquid").manifest,
            "копия лидера Hyperliquid",
        )
    if venue == "polymarket":
        from lab.strategies.prediction import make_pm_copy_strategy

        return make_pm_copy_strategy(ref).manifest, "копия кошелька Polymarket"
    if kind == "wallet" and chain:
        from lab.strategies.copy import make_copy_strategy

        return (
            make_copy_strategy(CHAIN_ALIASES.get(chain, chain), ref).manifest,
            "копия смарт-мани кошелька",
        )
    return None, f"нужен ручной манифест: {kind} · {venue or 'без площадки'}"


def candidate_hook(
    session_factory: Callable[[], Any],
    *,
    measure: Callable[..., Any] | None = None,
    ladder_factory: Callable[[Any], Any] | None = None,
    config: DiscoveryConfig | None = None,
) -> Callable[[str, str], None]:
    """Обработчик кнопок карточки для `bot.TraderBot(on_candidate=...)` (подключает T14)."""

    def hook(ref_id: str, decision: str) -> None:
        with session_factory() as session:
            ladder = ladder_factory(session) if ladder_factory else None
            result = decide(
                session,
                ref_id,
                decision,
                ladder=ladder,
                measure=measure,
                config=config,
            )
            log.info(
                "Кандидат #%s: %s (%s)", result.candidate_id, result.decision, result.reason
            )

    return hook


__all__ = [
    "CandidateNotFound",
    "DecisionResult",
    "candidate_hook",
    "decide",
    "manifest_for",
]
