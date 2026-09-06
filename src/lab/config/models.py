"""Pydantic-модели конфигов config/*.yaml (решение §16, В9а, В12, §11)."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.contracts import Branch


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BranchStop(_Cfg):
    """Предохранитель ветки: при пробое встают стратегии ветки, остальные работают."""

    loss_pct: Decimal = Field(gt=0, le=100)
    period: Literal["day", "week"]


class BranchGroupLimits(_Cfg):
    branches: list[Branch] = Field(min_length=1)
    share_pct: Decimal = Field(gt=0, le=100)
    max_trade_pct: Decimal = Field(gt=0, le=100)
    max_trade_base: Literal["bank", "branch"]
    max_leverage: Decimal = Field(ge=1)
    leverage_follows_leader: bool = False
    # Поддерживающая маржа площадки, %: расстояние до ликвидации ≈ 100/плечо − эта величина (R20.1).
    maintenance_margin_pct: Decimal = Field(ge=0, lt=100, default=Decimal("0.5"))
    stop: BranchStop


class LimitsConfig(_Cfg):
    real_capital_cap_usd: Decimal = Field(gt=0)
    groups: dict[str, BranchGroupLimits]

    @model_validator(mode="after")
    def _shares_sum_and_branches_unique(self) -> "LimitsConfig":
        total = sum(g.share_pct for g in self.groups.values())
        if total != Decimal(100):
            raise ValueError(f"groups: сумма share_pct должна быть 100, получено {total}")
        seen: dict[Branch, str] = {}
        for name, group in self.groups.items():
            for branch in group.branches:
                if branch in seen:
                    raise ValueError(
                        f"groups: ветка {branch} указана дважды ({seen[branch]}, {name})"
                    )
                seen[branch] = name
        missing = set(Branch) - set(seen)
        if missing:
            raise ValueError(f"groups: нет лимитов для веток {sorted(missing)}")
        return self

    def group_of(self, branch: Branch | str) -> str:
        wanted = Branch(branch)
        for name, group in self.groups.items():
            if wanted in group.branches:
                return name
        raise KeyError(branch)

    def for_branch(self, branch: Branch | str) -> BranchGroupLimits:
        return self.groups[self.group_of(branch)]


class ThresholdConfig(_Cfg):
    """Правило В12: ≥30 сделок, EV>0 после издержек, MaxDD в лимите, лучше BTC B&H."""

    min_trades: int = Field(ge=1)
    min_ev_after_costs: Decimal
    benchmark: str
    max_dd_pct: dict[str, Decimal]
    bootstrap_samples: int = Field(ge=100, default=1000)
    confidence: Decimal = Field(gt=0, lt=1, default=Decimal("0.95"))


class JobSpec(_Cfg):
    cron: str = Field(min_length=9)
    description: str = ""
    enabled: bool = True


class ScheduleConfig(_Cfg):
    timezone: str
    jobs: dict[str, JobSpec]
