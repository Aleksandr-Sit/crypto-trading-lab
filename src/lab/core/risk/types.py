"""Типы риск-ядра: вердикт, состояние ветки/стратегии, раскладка, запись изменения конфига."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lab.contracts import Branch, OrderIntent, Rung, Status, StopSpec
from lab.contracts.allocation import is_allocation


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Allow(_Model):
    """Ордер разрешён. Пустой объект — чтобы `check(...) == Allow()` читалось как в спецификации."""

    ok: Literal[True] = True

    def __bool__(self) -> bool:
        return True


class Deny(_Model):
    """Ордер отклонён: `reason` — по-русски для оператора, `rule` — код правила для журнала."""

    ok: Literal[False] = False
    reason: str
    rule: str

    def __bool__(self) -> bool:
        return False


Verdict = Allow | Deny


class BranchState(_Model):
    """Капитал ветки, как его видит риск-ядро. `stale` — площадка недоступна, цифры последние
    известные (R30i.5): ими считаем, но не обнуляем."""

    current_usd: Decimal
    exposure_usd: Decimal = Decimal(0)  # занятая маржа открытых позиций ветки
    pnl_day_pct: Decimal = Decimal(0)  # результат ветки за день, % от капитала на начало дня
    pnl_week_pct: Decimal = Decimal(0)
    stale: bool = False
    as_of: datetime | None = None


class StrategyStats(_Model):
    """Что нужно для стопов стратегии (G04): дневной результат и текущая просадка, в %."""

    pnl_day_pct: Decimal = Decimal(0)
    dd_pct: Decimal = Decimal(0)
    exposure_usd: Decimal = Decimal(0)


class StrategyInfo(_Model):
    """Срез записи реестра, нужный риск-ядру. `stop=None` — стратегия без стопа
    (на `micro`+ → Deny)."""

    id: str
    branch: Branch
    venue: str
    rung: Rung
    status: Status
    stop: StopSpec | None = None
    # Правило размещения (`allocation: true` в карточке): лимиты яруса вместо группы ветки.
    allocation: bool = False

    @classmethod
    def from_strategy(cls, s: Any) -> StrategyInfo:
        return cls(
            id=s.id,
            branch=Branch(s.branch),
            venue=s.venue,
            rung=Rung(s.rung),
            status=Status(s.status),
            stop=getattr(s, "stop", None),
            allocation=is_allocation(getattr(s, "params", None)),
        )


class Allocation(_Model):
    """Раскладка ветки: доля, база от банка, текущий капитал, занято, доступно."""

    branch: Branch
    group: str
    share_pct: Decimal
    base_usd: Decimal
    current_usd: Decimal
    exposure_usd: Decimal
    available_usd: Decimal
    max_trade_usd: Decimal
    max_leverage: Decimal
    stale: bool
    as_of: datetime | None = None


class ConfigChange(_Model):
    who: str
    when: datetime
    path: str
    diff: dict[str, Any] = Field(default_factory=dict)


class ReloadResult(_Model):
    applied: bool
    error: str | None = None
    change: ConfigChange | None = None


class Portfolio(Protocol):
    """Источник цифр для риск-ядра. Реализация — на стороне ops/executors; в тестах — фейк."""

    def bank_usd(self) -> Decimal: ...
    def branch(self, branch: Branch | str) -> BranchState: ...
    def venue_available(self, venue: str) -> bool: ...
    def strategy_stats(self, strategy_id: str) -> StrategyStats: ...
    def live_deployed_usd(self) -> Decimal: ...
    def allocation_exposure_usd(self) -> Decimal: ...  # занято стратегиями яруса размещения
    def mark_price(self, venue: str, instrument: str) -> Decimal | None: ...
    def liquidation_price(self, intent: OrderIntent) -> Decimal | None: ...


class HaltSwitch(Protocol):
    """Ручной «стоп всё» (G04.1). Один на систему; риск-ядро и лестница читают одно состояние."""

    def is_halted(self) -> bool: ...
    def halt(self, by: str) -> None: ...
    def resume(self, by: str) -> None: ...


class ConfigChangeSink(Protocol):
    def record(self, change: ConfigChange) -> None: ...
