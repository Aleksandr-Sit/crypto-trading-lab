"""Типы отбора кошельков: статистика, флаг накрутки, цена задержки копирования."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WalletStats(_Model):
    """Результат кошелька, пересчитанный по его сделкам (История 57).

    `currency` — валюта учёта: `USD`, если у всех сделок известна цена в долларах,
    иначе имя котируемого актива (TON-своп в USD без цены не переводим).
    """

    address: str
    chain: str
    n_trades: int = 0  # закрытых кругов «покупка → продажа»
    n_swaps: int = 0
    win_rate_pct: Decimal = Decimal(0)
    median_pnl: Decimal = Decimal(0)
    pnl_total: Decimal = Decimal(0)
    best_pnl: Decimal = Decimal(0)
    max_dd_pct: Decimal = Decimal(0)
    age_days: int = 0
    survived_pct: Decimal = Decimal(0)
    tokens: int = 0
    open_positions: int = 0
    volume: Decimal = Decimal(0)
    currency: str = "USD"
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None
    computed_at: datetime | None = None

    def passes(self, thresholds) -> bool:
        """Проходит ли кошелёк порог отбора (те же числа, что у переизмерения лидера)."""
        return (
            self.n_trades >= thresholds.min_trades
            and self.win_rate_pct >= thresholds.min_win_rate_pct
            and self.max_dd_pct <= thresholds.max_dd_pct
            and self.age_days >= thresholds.min_age_days
            and self.survived_pct >= thresholds.min_survived_pct
        )


class Flag(_Model):
    """Признак накрутки (История 58): код, человеческое объяснение, измеренная величина."""

    code: str
    detail: str
    value: Decimal | None = None


class LagCost(_Model):
    """Потеря при повторе сделки лидера через N секунд, базисные пункты (История 59)."""

    address: str
    chain: str
    by_delay_s: dict[int, Decimal] = Field(default_factory=dict)
    samples: dict[int, int] = Field(default_factory=dict)
    n_trades: int = 0

    def worst(self) -> Decimal:
        return max(self.by_delay_s.values()) if self.by_delay_s else Decimal(0)


__all__ = ["Flag", "LagCost", "WalletStats"]
