"""Лестница продаж (A05, История 68): частичные цели плюс трейлинг по остатку.

Цели живут в параметрах стратегии (по умолчанию — `config/meme.yaml`), а не в коде:
на ранней стадии шаг лестницы — это и есть гипотеза, которую мы меряем.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class LadderStep:
    gain_pct: Decimal
    sell_pct: Decimal


@dataclass(frozen=True)
class SellOrder:
    qty: Decimal
    reason: str
    gain_pct: Decimal
    peak_price: Decimal


@dataclass
class MemePosition:
    """Позиция по одному токену: сколько куплено, по какой цене и что уже продано."""

    token: str
    qty: Decimal = Decimal(0)
    initial_qty: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)
    peak_price: Decimal = Decimal(0)
    targets_done: set[int] = field(default_factory=set)
    opened_at: datetime | None = None
    signal_id: str = ""

    def add(self, qty: Decimal, price: Decimal, ts: datetime | None = None) -> None:
        total = self.qty + qty
        if total <= 0:
            return
        self.entry_price = (self.qty * self.entry_price + qty * price) / total
        self.qty = total
        self.initial_qty += qty
        self.peak_price = max(self.peak_price, price)
        self.opened_at = self.opened_at or ts or datetime.now(UTC)

    def reduce(self, qty: Decimal) -> None:
        self.qty = max(Decimal(0), self.qty - qty)


class SellLadder:
    """Правило выхода: цели по прибыли, трейлинг по остатку, стоп по убытку."""

    def __init__(
        self,
        steps: list[LadderStep],
        *,
        trailing_pct: Decimal = Decimal(25),
        stop_loss_pct: Decimal = Decimal(50),
    ) -> None:
        self.steps = steps
        self.trailing_pct = trailing_pct
        self.stop_loss_pct = stop_loss_pct

    def gain_pct(self, position: MemePosition, price: Decimal) -> Decimal:
        if position.entry_price <= 0:
            return Decimal(0)
        return (price - position.entry_price) / position.entry_price * 100

    def next_sell(self, position: MemePosition, price: Decimal) -> SellOrder | None:
        """Что продать при этой цене — не более одного шага за тик."""
        if position.qty <= 0 or price <= 0:
            return None
        position.peak_price = max(position.peak_price, price)
        gain = self.gain_pct(position, price)
        if self.stop_loss_pct > 0 and gain <= -self.stop_loss_pct:
            return SellOrder(position.qty, "stop_loss", gain, position.peak_price)
        for index, step in enumerate(self.steps):
            if index in position.targets_done or gain < step.gain_pct:
                continue
            qty = min(position.qty, position.initial_qty * step.sell_pct / 100)
            position.targets_done.add(index)
            if qty <= 0:
                return None
            return SellOrder(qty, f"ladder_target_{index + 1}", gain, position.peak_price)
        if not position.targets_done or self.trailing_pct <= 0:
            return None
        floor = position.peak_price * (100 - self.trailing_pct) / 100
        if price <= floor:
            return SellOrder(position.qty, "trailing_stop", gain, position.peak_price)
        return None


def steps_from_params(targets: list[dict] | None, defaults: list[LadderStep]) -> list[LadderStep]:
    if not targets:
        return defaults
    return [
        LadderStep(Decimal(str(t["gain_pct"])), Decimal(str(t["sell_pct"]))) for t in targets
    ]


__all__ = ["LadderStep", "MemePosition", "SellLadder", "SellOrder", "steps_from_params"]
