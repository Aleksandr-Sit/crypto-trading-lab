"""core.risk — одна дверь к ордеру (решение §8): `check(intent) -> Allow | Deny(reason, rule)`.

Правила (код правила = `Deny.rule`), в порядке проверки:
  halted · unknown_strategy · strategy_inactive · venue_unavailable · rung_mode · stop_missing ·
  strategy_stop_daily · strategy_stop_dd · branch_stop · no_price · leverage · max_trade ·
  branch_share · real_capital_cap · liquidation.

Ордер `reduce_only` (закрытие) проходит только первые шесть: закрыть позицию после пробоя стопа
можно всегда. Размер считается от текущего капитала ветки (R30i.3); доля ветки — по занятой марже
(`exposure_usd` + `notional / leverage`) против `share_pct × банк`; макс. на сделку — по номиналу.
Недоступная площадка → Deny для её ордеров, но капитал ветки остаётся последним известным (R30i.5).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from lab.config import CONFIG_DIR, ConfigError, LimitsConfig, load_config
from lab.contracts import RUNG_ORDER, Branch, Mode, OrderIntent, Rung, Status
from lab.core.registry import StrategyNotFound
from lab.core.risk.state import MemoryConfigLog, MemoryHaltSwitch, config_diff
from lab.core.risk.types import (
    Allocation,
    Allow,
    ConfigChange,
    ConfigChangeSink,
    Deny,
    HaltSwitch,
    Portfolio,
    ReloadResult,
    StrategyInfo,
    Verdict,
)
from lab.db.base import utcnow

StrategyLookup = Callable[[str], StrategyInfo | None]

_HUNDRED = Decimal(100)
_LIVE_RUNGS = frozenset(RUNG_ORDER[RUNG_ORDER.index(Rung.MICRO) :])
_INACTIVE = frozenset({Status.DEGRADED, Status.RETIRED})


def _fmt(x: Decimal) -> str:
    q = x.quantize(Decimal("0.01")) if x == x.to_integral() or abs(x) >= 1 else x
    return f"{q.normalize():f}"


def _pct(base: Decimal, pct: Decimal) -> Decimal:
    return base * pct / _HUNDRED


def registry_lookup(registry: Any) -> StrategyLookup:
    """Адаптер над `core.registry.Registry.get`: не найдено → None."""

    def lookup(strategy_id: str) -> StrategyInfo | None:
        try:
            return StrategyInfo.from_strategy(registry.get(strategy_id))
        except StrategyNotFound:
            return None

    return lookup


def real_capital_cap(limits: LimitsConfig) -> Decimal:
    """Потолок реального капитала (R30i.6): `REAL_CAPITAL_CAP` из окружения, иначе limits.yaml."""
    raw = os.environ.get("REAL_CAPITAL_CAP", "").strip()
    return Decimal(raw) if raw else limits.real_capital_cap_usd


class RiskEngine:
    def __init__(
        self,
        strategies: StrategyLookup,
        portfolio: Portfolio,
        *,
        limits: LimitsConfig | None = None,
        limits_path: Path | None = None,
        halt: HaltSwitch | None = None,
        change_log: ConfigChangeSink | None = None,
    ) -> None:
        self._lookup = strategies
        self._portfolio = portfolio
        self._path = Path(limits_path) if limits_path else CONFIG_DIR / "limits.yaml"
        self._limits = limits or load_config(self._path, LimitsConfig)
        self._halt = halt or MemoryHaltSwitch()
        self._log = change_log or MemoryConfigLog()

    # -- публичный интерфейс -----------------------------------------------------------

    @property
    def limits(self) -> LimitsConfig:
        return self._limits

    @property
    def halt_switch(self) -> HaltSwitch:
        return self._halt

    def check(self, intent: OrderIntent) -> Verdict:
        if self._halt.is_halted():
            return Deny(reason="Торговля остановлена командой «стоп всё» (/halt)", rule="halted")

        strategy = self._lookup(intent.strategy_id)
        if strategy is None:
            return Deny(
                reason=f"Стратегия {intent.strategy_id} не найдена в реестре",
                rule="unknown_strategy",
            )
        if strategy.status in _INACTIVE:
            return Deny(
                reason=f"Стратегия {strategy.id} в статусе {strategy.status}: ордера запрещены",
                rule="strategy_inactive",
            )
        if not self._portfolio.venue_available(intent.venue):
            return Deny(
                reason=f"Площадка {intent.venue} недоступна: ордер не отправлен, капитал ветки "
                "считается по последнему известному балансу",
                rule="venue_unavailable",
            )
        if intent.mode == Mode.LIVE and strategy.rung not in _LIVE_RUNGS:
            return Deny(
                reason=f"Ступень {strategy.rung} не допускает реальных ордеров — только бумага",
                rule="rung_mode",
            )
        if strategy.rung in _LIVE_RUNGS and strategy.stop is None:
            return Deny(
                reason=f"У стратегии {strategy.id} нет стопа, а ступень {strategy.rung} "
                "требует его в манифесте (stop: daily_pct / max_dd_pct)",
                rule="stop_missing",
            )

        limits = self._limits.for_branch(strategy.branch)
        branch = self._portfolio.branch(strategy.branch)
        stats = self._portfolio.strategy_stats(strategy.id)
        stop = strategy.stop

        if stop is not None and stop.daily_pct is not None and -stats.pnl_day_pct >= stop.daily_pct:
            return Deny(
                reason=f"Дневной стоп стратегии {strategy.id} пробит: {_fmt(stats.pnl_day_pct)} % "
                f"за день при лимите −{_fmt(stop.daily_pct)} %",
                rule="strategy_stop_daily",
            )
        if stop is not None and stop.max_dd_pct is not None and stats.dd_pct >= stop.max_dd_pct:
            return Deny(
                reason=f"Стоп по просадке стратегии {strategy.id} пробит: просадка "
                f"{_fmt(stats.dd_pct)} % при лимите {_fmt(stop.max_dd_pct)} %",
                rule="strategy_stop_dd",
            )
        branch_pnl = branch.pnl_day_pct if limits.stop.period == "day" else branch.pnl_week_pct
        if -branch_pnl >= limits.stop.loss_pct:
            period = "день" if limits.stop.period == "day" else "неделю"
            return Deny(
                reason=f"Стоп ветки {strategy.branch} пробит: {_fmt(branch_pnl)} % за {period} "
                f"при лимите −{_fmt(limits.stop.loss_pct)} % — стратегии ветки стоят",
                rule="branch_stop",
            )

        if intent.reduce_only:
            return Allow()

        price = intent.price or self._portfolio.mark_price(intent.venue, intent.instrument)
        if price is None or price <= 0:
            return Deny(
                reason=f"Нет цены для {intent.instrument} на {intent.venue}: размер не посчитать",
                rule="no_price",
            )
        if intent.leverage > limits.max_leverage:
            max_lev = limits.max_leverage
            lim = "без плеча" if max_lev == 1 else f"≤{_fmt(max_lev)}×"
            return Deny(
                reason=f"Плечо {_fmt(intent.leverage)}× выше лимита ветки "
                f"{strategy.branch} ({lim})",
                rule="leverage",
            )

        bank = self._portfolio.bank_usd()
        notional = intent.qty * price
        margin = notional / intent.leverage

        base = bank if limits.max_trade_base == "bank" else branch.current_usd
        base_name = "банка" if limits.max_trade_base == "bank" else "текущего капитала ветки"
        cap = _pct(base, limits.max_trade_pct)
        cap_text = f"{_fmt(limits.max_trade_pct)} % {base_name} = {_fmt(cap)} USD"
        if stop is not None and stop.max_position_pct is not None:
            strategy_cap = _pct(branch.current_usd, stop.max_position_pct)
            if strategy_cap < cap:
                cap = strategy_cap
                cap_text = (
                    f"{_fmt(stop.max_position_pct)} % капитала ветки по стопу стратегии = "
                    f"{_fmt(cap)} USD"
                )
        if notional > cap:
            return Deny(
                reason=f"Размер сделки {_fmt(notional)} USD больше максимума {cap_text}",
                rule="max_trade",
            )

        share_cap = _pct(bank, limits.share_pct)
        if branch.exposure_usd + margin > share_cap:
            return Deny(
                reason=f"Ветка {strategy.branch} выйдет за долю {_fmt(limits.share_pct)} % банка: "
                f"занято {_fmt(branch.exposure_usd)} + {_fmt(margin)} > {_fmt(share_cap)} USD",
                rule="branch_share",
            )

        if intent.mode == Mode.LIVE:
            cap_live = real_capital_cap(self._limits)
            deployed = self._portfolio.live_deployed_usd()
            if deployed + margin > cap_live:
                return Deny(
                    reason=f"Потолок реального капитала первой фазы {_fmt(cap_live)} USD: "
                    f"занято {_fmt(deployed)} + {_fmt(margin)} — сверх потолка только бумага",
                    rule="real_capital_cap",
                )

        if intent.leverage > 1 and stop is not None:
            stop_dist = stop.max_position_pct or stop.max_dd_pct or stop.daily_pct
            liq_price = self._portfolio.liquidation_price(intent)
            if liq_price is not None:
                liq_dist = abs(price - liq_price) / price * _HUNDRED
            else:
                liq_dist = _HUNDRED / intent.leverage - limits.maintenance_margin_pct
            if stop_dist is not None and liq_dist <= stop_dist:
                return Deny(
                    reason=f"Ликвидация ближе стопа: до ликвидации {_fmt(liq_dist)} %, стоп "
                    f"стратегии {_fmt(stop_dist)} % — позиция не открывается (R20.1)",
                    rule="liquidation",
                )

        return Allow()

    def allocation(self, branch: Branch | str) -> Allocation:
        b = Branch(branch)
        limits = self._limits.for_branch(b)
        state = self._portfolio.branch(b)
        bank = self._portfolio.bank_usd()
        base = _pct(bank, limits.share_pct)
        trade_base = bank if limits.max_trade_base == "bank" else state.current_usd
        return Allocation(
            branch=b,
            group=self._limits.group_of(b),
            share_pct=limits.share_pct,
            base_usd=base,
            current_usd=state.current_usd,
            exposure_usd=state.exposure_usd,
            available_usd=max(Decimal(0), base - state.exposure_usd),
            max_trade_usd=_pct(trade_base, limits.max_trade_pct),
            max_leverage=limits.max_leverage,
            stale=state.stale,
            as_of=state.as_of,
        )

    def reload(self, by: str = "operator") -> ReloadResult:
        """Перечитать limits.yaml без рестарта (R30i.4). Невалидный файл — старые лимиты
        остаются."""
        try:
            fresh = load_config(self._path, LimitsConfig)
        except ConfigError as err:
            return ReloadResult(applied=False, error=str(err))
        diff = config_diff(self._limits.model_dump(mode="json"), fresh.model_dump(mode="json"))
        self._limits = fresh
        if not diff:
            return ReloadResult(applied=True)
        change = ConfigChange(who=by, when=utcnow(), path=str(self._path), diff=diff)
        self._log.record(change)
        return ReloadResult(applied=True, change=change)
