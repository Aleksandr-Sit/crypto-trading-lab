"""Протокол источника данных для экрана `/feeds` (R13.3).

Веб читает `status()` и `budget()` — то, что спецификация закрепляет за
`ops.feeds_registry`. Реализацию даёт таск 14; здесь — типы и пустая заглушка,
чтобы экран жил без него. В тестах — фейк.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class FeedStatus(BaseModel):
    """Одна строка экрана источников: квота, здоровье, прогноз исчерпания, цена."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    kind: str = ""
    health: Literal["ok", "degraded", "down"] = "ok"
    health_detail: str = ""
    quota_used: int = 0
    quota_limit: int | None = None
    quota_period: str | None = None
    exhausted_at: datetime | None = None  # прогноз исчерпания квоты; None — не грозит
    cost_month: Decimal = Decimal(0)

    @property
    def quota_pct(self) -> int | None:
        if not self.quota_limit:
            return None
        return min(100, int(self.quota_used * 100 / self.quota_limit))


class Budget(BaseModel):
    """Бюджетомер платных источников (A01): лимит, потрачено, прогноз на конец месяца."""

    model_config = ConfigDict(frozen=True)

    month_limit_usd: Decimal
    spent_usd: Decimal
    forecast_usd: Decimal

    @property
    def warning(self) -> bool:
        return self.forecast_usd >= self.month_limit_usd * Decimal("0.8")


@runtime_checkable
class FeedsStatusSource(Protocol):
    def status(self) -> list[FeedStatus]: ...

    def budget(self) -> Budget: ...


class NoFeedsSource:
    """Пока реестр источников не подключён: пустой список и нулевой бюджет."""

    def __init__(self, month_limit_usd: Decimal = Decimal(50)) -> None:
        self._limit = month_limit_usd

    def status(self) -> list[FeedStatus]:
        return []

    def budget(self) -> Budget:
        return Budget(month_limit_usd=self._limit, spent_usd=Decimal(0), forecast_usd=Decimal(0))
