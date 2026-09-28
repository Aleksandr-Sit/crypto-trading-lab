"""Портфель для риск-ядра (`core.risk.Portfolio`, R30i, R30i.5).

Собирается из того, что уже есть: балансы и позиции — у исполнителей площадок,
P&L — из журнала (`trades`), доли веток — из `config/limits.yaml`. Снимок раскладки
пишется в таблицу `allocations` (её читает веб).

Главное правило R30i.5: **недоступная площадка не обнуляет капитал.** Последний
удачный ответ каждой площадки лежит в `system_flags` (`venue_balance:<venue>`); при
обрыве возвращается он с `stale=True`, а ветка, где есть такая площадка, помечается
`stale` — риск-ядро считает по последним известным цифрам, но знает, что они не свежие.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from lab.config import load_limits
from lab.contracts import Branch, OrderIntent
from lab.contracts.allocation import is_allocation, peak_drawdown_pct
from lab.core.risk import BranchState, StrategyStats
from lab.db.models import AllocationRow, StrategyRow, SystemFlagRow, TradeRow

log = logging.getLogger(__name__)

STABLES = ("USDT", "USDC", "USD", "DAI", "BUSD", "FDUSD")
BALANCE_FLAG = "venue_balance:"
#: Вершина результата стратегии яруса размещения, USD. При ручном возврате стратегии
#: из `degraded` оператором запись удаляется — иначе стоп сработает снова сразу же.
PEAK_FLAG = "strategy_peak:"

#: Какие площадки формируют капитал ветки. Площадки, которых нет среди исполнителей,
#: просто не участвуют — ветка тогда живёт на своей доле банка.
BRANCH_VENUES: dict[str, tuple[str, ...]] = {
    "cex-spot": ("bybit", "okx", "binance"),
    "cex-perp": ("bybit", "okx", "binance"),
    "dex-perp": ("hyperliquid",),
    "copy": ("hyperliquid", "bybit"),
    "meme": ("solana", "jupiter"),
    "nft": ("magiceden", "opensea"),
    "prediction": ("polymarket",),
    "rh": ("robinhood",),
}


@dataclass(frozen=True)
class VenueBalance:
    venue: str
    usd: Decimal
    stale: bool
    as_of: datetime | None


class LivePortfolio:
    """Реализация протокола `core.risk.Portfolio` поверх исполнителей и журнала."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        executors: Mapping[str, Any],
        limits: Any = None,
        branch_venues: Mapping[str, tuple[str, ...]] | None = None,
        clock: Callable[[], datetime] | None = None,
        mode: str = "live",
    ) -> None:
        self._sf = session_factory  # фабрика контекста сессии: `with session_factory() as s`
        self.executors = dict(executors)
        self.limits = limits or load_limits()
        self.branch_venues = dict(branch_venues or BRANCH_VENUES)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.mode = mode
        self._unavailable: set[str] = set()

    # -- балансы -------------------------------------------------------------------------

    def venue_balance(self, venue: str) -> VenueBalance:
        executor = self.executors.get(venue)
        if executor is not None:
            try:
                usd, as_of, stale = self._read_balance(executor)
                self._unavailable.discard(venue)
                if not stale:
                    self._remember(venue, usd, as_of)
                    return VenueBalance(venue, usd, False, as_of)
                self._unavailable.add(venue)
                return VenueBalance(venue, usd, True, as_of)
            except Exception as err:  # noqa: BLE001 — обрыв площадки: берём последнее известное
                log.warning("Баланс %s недоступен: %s", venue, err)
                self._unavailable.add(venue)
        known = self._last_known(venue)
        return VenueBalance(venue, known[0], True, known[1])

    def _read_balance(self, executor: Any) -> tuple[Decimal, datetime | None, bool]:
        total, as_of, stale = Decimal(0), None, False
        for item in executor.balance() or []:
            stale = stale or bool(getattr(item, "stale", False))
            as_of = getattr(item, "as_of", None) or as_of
            if item.asset.upper() in STABLES:
                total += Decimal(item.total)
                continue
            price = self._mark(executor, f"{item.asset}/USDT")
            if price is not None:
                total += Decimal(item.total) * price
        return total, as_of, stale

    def _mark(self, executor: Any, instrument: str) -> Decimal | None:
        for name in ("mark_price", "ticker"):
            fn = getattr(executor, name, None)
            if fn is None:
                continue
            try:
                value = fn(instrument)
            except Exception:  # noqa: BLE001
                return None
            if isinstance(value, Mapping):
                value = value.get("last") or value.get("bid")
            if value is not None:
                return Decimal(str(value))
        return None

    def _remember(self, venue: str, usd: Decimal, as_of: datetime | None) -> None:
        with self._sf() as session:
            key = f"{BALANCE_FLAG}{venue}"
            row = session.get(SystemFlagRow, key)
            if row is None:
                row = SystemFlagRow(key=key, updated_by="portfolio")
                session.add(row)
            row.value = {"usd": str(usd), "as_of": (as_of or self.clock()).isoformat()}

    def _last_known(self, venue: str) -> tuple[Decimal, datetime | None]:
        with self._sf() as session:
            row = session.get(SystemFlagRow, f"{BALANCE_FLAG}{venue}")
            if row is None or not row.value:
                return Decimal(0), None
            as_of = row.value.get("as_of")
            return Decimal(row.value.get("usd", "0")), (
                datetime.fromisoformat(as_of) if as_of else None
            )

    def bank_usd(self) -> Decimal:
        return sum((self.venue_balance(v).usd for v in self.executors), Decimal(0))

    def venue_available(self, venue: str) -> bool:
        executor = self.executors.get(venue)
        if executor is None:
            return False
        if venue in self._unavailable:
            return False
        health_fn = getattr(executor, "health", None)
        if health_fn is None:
            return True
        try:
            health = health_fn()
        except Exception:  # noqa: BLE001
            return False
        return str(getattr(health, "status", "ok")) != "down"

    # -- ветки ---------------------------------------------------------------------------

    def shares(self) -> dict[str, Decimal]:
        """Доля каждой ветки: доля группы из `limits.yaml`, поровну между её ветками.
        Остаток от деления отдаётся последней ветке группы — сумма долей ровно 100%."""
        out: dict[str, Decimal] = {}
        for group in self.limits.groups.values():
            branches = [str(getattr(b, "value", b)) for b in group.branches]
            if not branches:
                continue
            each = (Decimal(group.share_pct) / len(branches)).quantize(Decimal("0.0001"))
            for name in branches[:-1]:
                out[name] = each
            out[branches[-1]] = Decimal(group.share_pct) - each * (len(branches) - 1)
        return out

    def share_pct(self, branch: Branch | str) -> Decimal:
        name = str(getattr(branch, "value", branch))
        shares = self.shares()
        if name not in shares:
            raise KeyError(name)
        return shares[name]

    def _branch_venues(self, branch: str) -> list[str]:
        return [v for v in self.branch_venues.get(branch, ()) if v in self.executors]

    def branch(self, branch: Branch | str) -> BranchState:
        name = Branch(branch).value if not isinstance(branch, str) else str(branch)
        venues = self._branch_venues(name)
        balances = [self.venue_balance(v) for v in venues]
        stale = any(b.stale for b in balances)
        base = self.share_pct(name) * self.bank_usd() / Decimal(100)
        current = sum((b.usd for b in balances), Decimal(0)) if balances else base
        as_of = max((b.as_of for b in balances if b.as_of), default=None)
        day = self._pnl_pct(name, days=1, base=current or base)
        week = self._pnl_pct(name, days=7, base=current or base)
        return BranchState(
            current_usd=current,
            exposure_usd=self._exposure(name, venues),
            pnl_day_pct=day,
            pnl_week_pct=week,
            stale=stale,
            as_of=as_of,
        )

    def _exposure(self, branch: str, venues: list[str]) -> Decimal:
        total = Decimal(0)
        for venue in venues:
            executor = self.executors.get(venue)
            try:
                positions = executor.positions() or []
            except Exception:  # noqa: BLE001 — недоступная площадка: экспозицию считаем по журналу
                positions = []
            for p in positions:
                leverage = Decimal(getattr(p, "leverage", 1) or 1)
                total += Decimal(p.qty) * Decimal(p.entry_price) / leverage
        if total:
            return total
        # Позиции площадки по стратегиям не разложить, поэтому ярус размещения отделяется
        # только здесь, по журналу. Ярус — спотовый (ротация), а позиции площадки — перпы.
        rows = self._branch_trades(branch, TradeRow.closed_at.is_(None))
        return sum((r.qty * r.entry_price for r in rows), Decimal(0))

    def _branch_trades(self, branch: str, *where: Any) -> list[TradeRow]:
        """Сделки ветки БЕЗ стратегий яруса размещения: у яруса своя доля, и его просадка
        не должна вставать стопом ветки для соседних правил (решение 4, 27.09.2026)."""
        with self._sf() as session:
            pairs = session.execute(
                select(TradeRow, StrategyRow.params_json)
                .join(StrategyRow, StrategyRow.id == TradeRow.strategy_id)
                .where(StrategyRow.branch == branch, *where)
            ).all()
        return [trade for trade, params in pairs if not is_allocation(params)]

    def _pnl_pct(self, branch: str, *, days: int, base: Decimal) -> Decimal:
        if base <= 0:
            return Decimal(0)
        edge = self.clock() - timedelta(days=days)
        rows = self._branch_trades(
            branch, TradeRow.closed_at.is_not(None), TradeRow.closed_at >= edge
        )
        pnl = sum((r.pnl_net for r in rows), Decimal(0))
        return (pnl * 100 / base).quantize(Decimal("0.0001"))

    # -- ярус размещения -----------------------------------------------------------------

    def allocation_base_usd(self) -> Decimal:
        """Доля яруса размещения в долларах: `allocation_tier.share_pct` от банка."""
        tier = getattr(self.limits, "allocation_tier", None)
        if tier is None:
            return Decimal(0)
        return self.bank_usd() * Decimal(tier.share_pct) / Decimal(100)

    def allocation_exposure_usd(self) -> Decimal:
        """Занято стратегиями яруса: открытые лоты по цене входа (как `_exposure` по журналу)."""
        with self._sf() as session:
            pairs = session.execute(
                select(TradeRow, StrategyRow.params_json)
                .join(StrategyRow, StrategyRow.id == TradeRow.strategy_id)
                .where(TradeRow.closed_at.is_(None))
            ).all()
        return sum(
            (t.qty * t.entry_price for t, params in pairs if is_allocation(params)), Decimal(0)
        )

    def _unrealized(self, row: TradeRow, venue: str) -> Decimal:
        price = self.mark_price(row.venue or venue, row.instrument)
        if price is None:
            # Нет цены — позиция видна по входу. Стоп тогда слепнет к ходу цены, поэтому
            # это предупреждение, а не тихий ноль.
            log.warning("Нет цены %s на %s: просадка яруса по цене входа", row.instrument, venue)
            return Decimal(0)
        move = (price - row.entry_price) * row.qty
        return move if row.side == "long" else -move

    def _allocation_drawdown(self, strategy: StrategyRow, rows: list[TradeRow]) -> Decimal:
        """Просадка стратегии яруса: от ВЕРШИНЫ её капитала, с открытыми лотами по текущей
        цене (`lab.contracts.allocation`). Капитал — доля яруса плюс результат стратегии;
        вершина результата хранится в `system_flags` и переживает рестарт — иначе после
        перезапуска просадка считалась бы от сегодняшней цены, а не от вершины."""
        base = self.allocation_base_usd()
        if base <= 0:
            return Decimal(0)
        pnl = sum((r.pnl_net for r in rows if r.closed_at is not None), Decimal(0))
        pnl += sum(
            (self._unrealized(r, strategy.venue) for r in rows if r.closed_at is None),
            Decimal(0),
        )
        known = self._peak(strategy.id)
        peak = max(known, pnl, Decimal(0))
        if peak != known:
            self._save_peak(strategy.id, peak)
        return peak_drawdown_pct(base + peak, base + pnl).quantize(Decimal("0.0001"))

    def _peak(self, strategy_id: str) -> Decimal:
        with self._sf() as session:
            row = session.get(SystemFlagRow, _peak_key(strategy_id))
            if row is None or not row.value:
                return Decimal(0)
            return Decimal(row.value.get("pnl", "0"))

    def _save_peak(self, strategy_id: str, peak: Decimal) -> None:
        with self._sf() as session:
            key = _peak_key(strategy_id)
            row = session.get(SystemFlagRow, key)
            if row is None:
                row = SystemFlagRow(key=key, updated_by="portfolio")
                session.add(row)
            row.value = {
                "strategy_id": strategy_id,
                "pnl": str(peak),
                "as_of": self.clock().isoformat(),
            }

    # -- стратегии -----------------------------------------------------------------------

    def strategy_stats(self, strategy_id: str) -> StrategyStats:
        edge = self.clock() - timedelta(days=1)
        with self._sf() as session:
            strategy = session.get(StrategyRow, strategy_id)
            rows = session.scalars(
                select(TradeRow)
                .where(TradeRow.strategy_id == strategy_id)
                .order_by(TradeRow.opened_at, TradeRow.id)
            ).all()
        tiered = strategy is not None and is_allocation(strategy.params_json)
        if strategy is None:
            base = Decimal(0)
        elif tiered:
            base = self.allocation_base_usd()
        else:
            base = self.branch(strategy.branch).current_usd
        day = sum(
            (r.pnl_net for r in rows if r.closed_at is not None and r.closed_at >= edge),
            Decimal(0),
        )
        exposure = sum(
            (r.qty * r.entry_price for r in rows if r.closed_at is None), Decimal(0)
        )
        dd = (
            self._allocation_drawdown(strategy, list(rows))
            if tiered and strategy is not None
            else _drawdown_pct(rows, base)
        )
        return StrategyStats(
            pnl_day_pct=(day * 100 / base).quantize(Decimal("0.0001")) if base > 0 else Decimal(0),
            dd_pct=dd,
            exposure_usd=exposure,
        )

    def live_deployed_usd(self) -> Decimal:
        with self._sf() as session:
            rows = session.scalars(
                select(TradeRow).where(TradeRow.mode == "live", TradeRow.closed_at.is_(None))
            ).all()
        return sum((r.qty * r.entry_price for r in rows), Decimal(0))

    # -- цены ----------------------------------------------------------------------------

    def mark_price(self, venue: str, instrument: str) -> Decimal | None:
        executor = self.executors.get(venue)
        if executor is None:
            return None
        return self._mark(executor, instrument)

    def liquidation_price(self, intent: OrderIntent) -> Decimal | None:
        executor = self.executors.get(intent.venue)
        info = getattr(executor, "perp_info", None)
        if info is None:
            return None
        try:
            return getattr(info(intent.instrument), "liquidation_price", None)
        except Exception:  # noqa: BLE001
            return None

    # -- снимок раскладки ----------------------------------------------------------------

    def refresh(self, *, risk: Any = None, now: datetime | None = None) -> list[AllocationRow]:
        """Записать таблицу `allocations` (её читает веб): доля, база, текущий капитал."""
        at = now or self.clock()
        bank = self.bank_usd()
        out: list[AllocationRow] = []
        with self._sf() as session:
            for branch in Branch:
                name = branch.value
                try:
                    share = self.share_pct(name)
                except KeyError:
                    continue
                state = self.branch(name)
                base = share * bank / Decimal(100)
                if risk is not None:
                    try:
                        allocation = risk.allocation(name)
                        share, base = allocation.share_pct, allocation.base_usd
                    except Exception as err:  # noqa: BLE001
                        log.info("Раскладка %s от риск-ядра недоступна: %s", name, err)
                row = session.get(AllocationRow, name)
                if row is None:
                    row = AllocationRow(branch=name, share=share, base_amount=base)
                    session.add(row)
                row.share, row.base_amount = share, base
                row.current_amount = state.current_usd
                row.stale, row.updated_at = state.stale, at
                out.append(row)
            session.flush()
        return out

    def allocation_job(self, *, risk: Any = None, cron: str = "*/15 * * * *"):
        from lab.ops.scheduler import Job

        return Job(
            id="allocations",
            func=lambda: self.refresh(risk=risk),
            cron=cron,
            description="Снимок капитала по веткам в таблицу allocations",
        )


def _peak_key(strategy_id: str) -> str:
    """Ключ вершины в `system_flags` (ключ — до 64 знаков, id стратегии — до 128)."""
    key = f"{PEAK_FLAG}{strategy_id}"
    if len(key) <= 64:
        return key
    return f"{PEAK_FLAG}{hashlib.sha256(strategy_id.encode()).hexdigest()[:40]}"


def _drawdown_pct(rows: list[TradeRow], base: Decimal) -> Decimal:
    """Просадка по кривой закрытых сделок, % от капитала ветки."""
    if base <= 0:
        return Decimal(0)
    equity, peak, worst = Decimal(0), Decimal(0), Decimal(0)
    for row in rows:
        if row.closed_at is None:
            continue
        equity += row.pnl_net
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return (-worst * 100 / base).quantize(Decimal("0.0001"))


__all__ = ["BRANCH_VENUES", "STABLES", "LivePortfolio", "VenueBalance"]
