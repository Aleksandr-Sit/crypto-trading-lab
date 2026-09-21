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

from sqlalchemy import select

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


EXPIRE_REASON = "срок годности: лежал без решения"


def expire_candidates(session, *, days: int, now: datetime | None = None) -> list[int]:
    """Кандидаты, пролежавшие без решения дольше `days` суток, → `rejected`.

    Сама система по кандидату решения не принимает никогда — ни через неделю, ни через
    месяц, — а поиск идёт каждый понедельник, поэтому очередь может только расти: к
    21.09.2026 в ней лежало 5075 записей и ни одного решения. Отклонение здесь не приговор:
    источник вернёт кандидата сам, если существенно изменится отпечаток (`scan`, R15.2).
    """
    if days <= 0:
        return []
    at = now or utcnow()
    edge = at - timedelta(days=days)
    rows = list(
        session.scalars(
            select(CandidateRow).where(
                CandidateRow.decision == CandidateDecision.PENDING.value,
                CandidateRow.discovered_at < edge,
            )
        )
    )
    for row in rows:
        row.decision = CandidateDecision.REJECTED.value
        row.decided_at = at
        row.payload = {
            **dict(row.payload or {}),
            "reject_reason": f"{EXPIRE_REASON} {days} сут.",
        }
    session.flush()
    return [row.id for row in rows]


@dataclass(frozen=True)
class DecisionResult:
    candidate_id: int
    decision: CandidateDecision
    strategy_id: str | None = None
    mode: MeasureMode | None = None
    reason: str = ""
    measured: bool = False
    measurement: Any = None

    def text(self) -> str:
        """Строка для карточки бота: что решено и чем кончился замер."""
        if self.decision is CandidateDecision.REJECTED:
            return f"Отклонён: {self.reason}" if self.reason else "Отклонён"
        if self.decision is CandidateDecision.PENDING:
            return "Отложен — вернётся в очередь"
        head = f"В замер: {self.strategy_id or self.reason}"
        if self.mode is not None:
            head += f" · {self.mode}"
        if not self.measured:
            return f"{head} — замер не запускался ({self.reason})" if self.reason else head
        return f"{head} — {_measure_text(self.measurement)}"


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
    measured, measurement = False, None
    if measure is not None:
        window = (at - timedelta(days=cfg.remeasure.window_days), at)
        measurement = measure(strategy_id=strategy_id, mode=mode.value, window=window)
        measured = True
    return DecisionResult(
        row.id,
        CandidateDecision.ACCEPTED,
        strategy_id=strategy_id,
        mode=mode,
        reason=plan.reason,
        measured=measured,
        measurement=measurement,
    )


def _measure_text(measurement: Any) -> str:
    """Итог замера коротко: статус, порог, причина неполноты."""
    if measurement is None:
        return "замер запущен"
    status = getattr(measurement, "status", None)
    if status is None:
        return str(measurement)
    if status != "ok":
        return f"замер неполный: {getattr(measurement, 'reason', '') or 'нет данных'}"
    threshold = getattr(measurement, "threshold", None)
    if threshold is None:
        return "замер выполнен"
    failed = ", ".join(threshold.failed_names()) or "—"
    return f"замер выполнен, порог: {threshold.status} (не прошло: {failed})"


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
) -> Callable[[str, str], str]:
    """Обработчик кнопок карточки для `bot.TraderBot(on_candidate=...)`.

    Возврат — строка для карточки: что решено и чем кончился замер. Без `measure=`
    кнопка «В замер» только заводит стратегию, и это видно в тексте."""

    def hook(ref_id: str, decision: str) -> str:
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
            return result.text()

    return hook


__all__ = [
    "EXPIRE_REASON",
    "CandidateNotFound",
    "DecisionResult",
    "candidate_hook",
    "decide",
    "expire_candidates",
    "manifest_for",
]
