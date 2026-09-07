"""Утренний отчёт (R13): P&L за сутки по веткам, переходы по лестнице, ожидающие решения,
здоровье источников. Данные — через интерфейсы ядра (`Journal`, `Ladder`, `Registry`);
реестра источников (`ops.feeds_registry`, тикет 08/14) ещё нет — протокол + честное «н/д».
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import SignalOutcome
from lab.core.journal import Journal
from lab.core.ladder import Ladder, Transition
from lab.core.registry import Registry
from lab.db.models import SignalRow, StrategyRow

SAMARA = ZoneInfo("Europe/Samara")
SIGNAL_RUNGS = ("signal", "semi")


class FeedStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    feed_id: str
    health: str  # ok | degraded | down
    detail: str = ""
    quota_used: int | None = None
    quota_limit: int | None = None


class FeedsStatus(Protocol):
    def status(self) -> Sequence[FeedStatus]: ...


class MorningReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    pending_cards: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)


def _fmt(d: Decimal) -> str:
    sign = "+" if d > 0 else ""
    return f"{sign}{d.quantize(Decimal('0.01'))}"


def pending_signal_cards(s: Session) -> list[tuple[str, dict[str, Any]]]:
    """Сигналы без ответа на ступенях signal/semi — как payload карточек `signal`."""
    stmt = (
        select(SignalRow, StrategyRow)
        .join(StrategyRow, StrategyRow.id == SignalRow.strategy_id)
        .where(
            SignalRow.outcome == SignalOutcome.PENDING.value,
            StrategyRow.rung.in_(SIGNAL_RUNGS),
        )
        .order_by(SignalRow.decided_at)
    )
    cards: list[tuple[str, dict[str, Any]]] = []
    for sig, strat in s.execute(stmt).all():
        cards.append(
            (
                "signal",
                {
                    "signal_id": sig.id,
                    "strategy_id": strat.id,
                    "rung": strat.rung,
                    "instrument": sig.instrument,
                    "side": sig.side,
                    "size": str(sig.size),
                    "price_ref": str(sig.price_ref) if sig.price_ref is not None else None,
                    "venue": strat.venue,
                    "ttl_s": int(strat.params_json.get("ttl_s", sig.ttl)),
                },
            )
        )
    return cards


def build_morning_report(
    s: Session,
    *,
    ladder: Ladder,
    feeds: FeedsStatus | None,
    now: datetime,
    window: timedelta = timedelta(hours=24),
) -> MorningReport:
    since = now - window
    registry, journal = Registry(s), Journal(s)
    strategies = registry.list()

    # P&L за сутки по веткам: закрытые за окно сделки + нереализованное на сейчас
    day_by_branch: dict[str, Decimal] = {}
    total_by_branch: dict[str, Decimal] = {}
    for st in strategies:
        branch = str(st.branch)
        closed = [t for t in journal.closed_trades(st.id) if t.closed_at and t.closed_at >= since]
        day_by_branch[branch] = day_by_branch.get(branch, Decimal(0)) + sum(
            (t.pnl_net for t in closed), Decimal(0)
        )
        total_by_branch[branch] = total_by_branch.get(branch, Decimal(0)) + journal.pnl(st.id).total

    transitions: list[Transition] = []
    for st in strategies:
        transitions += [t for t in ladder.history(st.id) if t.ts >= since]
    transitions.sort(key=lambda t: t.ts)

    pending = pending_signal_cards(s)

    lines = [f"☀️ <b>Утренний отчёт</b> · {now.astimezone(SAMARA):%d.%m.%Y %H:%M} Самара"]
    lines.append("<b>Стоп всё:</b> " + ("ДА — торговля остановлена" if ladder.halted else "нет"))

    lines.append("<b>P&amp;L за сутки по веткам</b>")
    if not day_by_branch:
        lines.append("• н/д — стратегий в реестре нет")
    for branch in sorted(day_by_branch):
        lines.append(
            f"• {branch}: {_fmt(day_by_branch[branch])} USD за сутки, "
            f"всего {_fmt(total_by_branch[branch])} USD"
        )

    lines.append("<b>Лестница за сутки</b>")
    if not transitions:
        lines.append("• переходов не было")
    for t in transitions:
        lines.append(f"• {t.strategy_id}: {t.from_rung} → {t.to_rung} [{t.status}] — {t.reason}")

    lines.append(f"<b>Ждут решения:</b> {len(pending)}" + (" — карточки ниже" if pending else ""))

    lines.append("<b>Источники</b>")
    if feeds is None:
        lines.append("• н/д — реестр источников ещё не подключён")
    else:
        statuses = list(feeds.status())
        if not statuses:
            lines.append("• н/д — источники не зарегистрированы")
        for f in statuses:
            quota = (
                f", квота {f.quota_used}/{f.quota_limit}" if f.quota_limit is not None else ""
            )
            mark = {"ok": "🟢", "degraded": "🟡"}.get(f.health, "🔴")
            detail = f" — {f.detail}" if f.detail else ""
            lines.append(f"• {mark} {f.feed_id}: {f.health}{quota}{detail}")

    return MorningReport(text="\n".join(lines), pending_cards=pending)
