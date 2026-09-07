"""Индекс внимания («хайп») — измеримая величина, а не ощущение (История 74a, R23.2).

Компоненты нормированы в [0, 1], итог — взвешенная сумма. Веса лежат в `config/nft.yaml`
и помечены `hypothesis: true`: они сами предмет замера, поэтому значение индекса всегда
несёт с собой и веса, которыми посчитано, и эту пометку — чтобы карточку минта нельзя
было прочитать как «система знает, что это хайп».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from lab.feeds.nft.config import NftConfig, load_nft

ZERO = Decimal(0)
ONE = Decimal(1)


def clamp01(value: Decimal | float | int | None) -> Decimal:
    if value is None:
        return ZERO
    dec = Decimal(str(value))
    if dec < ZERO:
        return ZERO
    return ONE if dec > ONE else dec


@dataclass(frozen=True)
class AttentionComponents:
    """Сырьё индекса. Чего нет — то ноль, а не «пропустим компонент»."""

    mentions_growth: Decimal = ZERO
    allowlist_demand: Decimal = ZERO
    launchpad_fill: Decimal = ZERO
    creator_score: Decimal = ZERO

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "mentions_growth": clamp01(self.mentions_growth),
            "allowlist_demand": clamp01(self.allowlist_demand),
            "launchpad_fill": clamp01(self.launchpad_fill),
            "creator_score": clamp01(self.creator_score),
        }


@dataclass(frozen=True)
class AttentionIndex:
    value: Decimal
    components: dict[str, Decimal]
    weights: dict[str, Decimal]
    hypothesis: bool = True
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def note(self) -> str:
        return "веса индекса — гипотеза, сами предмет замера" if self.hypothesis else ""


def mentions_growth(before: int, after: int, *, cap: Decimal = Decimal(3)) -> Decimal:
    """Скорость роста упоминаний: (после − до) / max(до, 1), срезано потолком `cap`."""
    base = Decimal(max(before, 1))
    growth = (Decimal(after) - Decimal(before)) / base
    if growth <= ZERO:
        return ZERO
    return clamp01(growth / cap)


def allowlist_demand(seats: int | None, demand: int | None) -> Decimal:
    """Спрос на allowlist-места к их числу: чем больше желающих на место, тем выше."""
    if not seats or demand is None:
        return ZERO
    ratio = Decimal(demand) / Decimal(seats)
    if ratio <= ONE:
        return ZERO
    return clamp01((ratio - ONE) / Decimal(9))  # 10 желающих на место → 1.0


def attention_index(
    components: AttentionComponents,
    *,
    config: NftConfig | None = None,
) -> AttentionIndex:
    cfg = config or load_nft()
    weights = cfg.attention.weights.as_dict()
    values = components.as_dict()
    total = sum((weights[name] * values[name] for name in weights), start=ZERO)
    return AttentionIndex(
        value=total,
        components=values,
        weights=weights,
        hypothesis=cfg.attention.hypothesis,
    )
