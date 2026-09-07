"""Позиция в NFT: частичная продажа на целях, остаток на удержание, неликвид.

История 78 (G06): «часть будет продаваться на одних значениях, часть будем оставлять для
большего роста» — поэтому доля `hold_pct` из лестницы исключена: её не продаст ни одна
цель, только явное решение оператора.

История 80: если покупателей нет, позиция получает флаг неликвида с возрастом и правилом
снижения цены — шаг за шагом, но не ниже пола от цены входа.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds.nft.config import IlliquidConfig, LadderConfig, NftConfig, load_nft

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass
class NftPosition:
    collection: str
    qty: Decimal
    entry_price: Decimal
    opened_at: datetime
    token_id: str | None = None
    market: str = ""
    chain: str = ""
    strategy_id: str = ""
    sold_qty: Decimal = ZERO
    listed_price: Decimal | None = None
    listed_at: datetime | None = None
    meta: dict = field(default_factory=dict)

    @property
    def open_qty(self) -> Decimal:
        return self.qty - self.sold_qty

    @property
    def sold_pct(self) -> Decimal:
        return ZERO if self.qty <= ZERO else self.sold_qty / self.qty * HUNDRED

    def age_days(self, now: datetime | None = None) -> Decimal:
        at = now or datetime.now(UTC)
        opened = self.opened_at if self.opened_at.tzinfo else self.opened_at.replace(tzinfo=UTC)
        return Decimal((at - opened).total_seconds()) / Decimal(86400)

    def gain_pct(self, price: Decimal) -> Decimal:
        if self.entry_price <= ZERO:
            return ZERO
        return (price - self.entry_price) / self.entry_price * HUNDRED


@dataclass(frozen=True)
class SellOrder:
    """Что выставить на продажу прямо сейчас."""

    collection: str
    token_id: str | None
    qty: Decimal
    price: Decimal
    reason: str
    gain_pct: Decimal = ZERO
    kind: str = "target"  # target | stop | markdown


@dataclass(frozen=True)
class HoldPlan:
    """Остаток на удержание — доля, которую лестница не трогает."""

    qty: Decimal
    pct: Decimal
    reason: str = "остаток на удержание (G06)"


def _ladder(config: NftConfig | LadderConfig | None) -> LadderConfig:
    if isinstance(config, LadderConfig):
        return config
    return (config or load_nft()).ladder


def hold_plan(position: NftPosition, *, config: NftConfig | LadderConfig | None = None) -> HoldPlan:
    ladder = _ladder(config)
    qty = position.qty * ladder.hold_pct / HUNDRED
    return HoldPlan(qty=qty, pct=ladder.hold_pct)


def sell_plan(
    position: NftPosition,
    price: Decimal,
    *,
    config: NftConfig | LadderConfig | None = None,
) -> list[SellOrder]:
    """Что продать при текущей цене: все достигнутые цели минус уже проданное.

    Продаваемая доля — 100 − `hold_pct`; цели сверх неё срезаются. Стоп-лосс продаёт всю
    продаваемую часть, удержание не трогает: остаток — сознательная ставка, а не забытая.
    """
    ladder = _ladder(config)
    if position.open_qty <= ZERO or position.qty <= ZERO:
        return []
    gain = position.gain_pct(price)
    sellable_pct = max(ZERO, HUNDRED - ladder.hold_pct)

    if ladder.stop_loss_pct > ZERO and gain <= -ladder.stop_loss_pct:
        qty = position.qty * sellable_pct / HUNDRED - position.sold_qty
        if qty <= ZERO:
            return []
        return [
            SellOrder(
                collection=position.collection,
                token_id=position.token_id,
                qty=qty,
                price=price,
                gain_pct=gain,
                reason=f"стоп −{ladder.stop_loss_pct}%",
                kind="stop",
            )
        ]

    reached = ZERO
    hit: list[tuple[Decimal, Decimal]] = []
    for target in ladder.targets:
        if gain >= target.gain_pct:
            reached += target.sell_pct
            hit.append((target.gain_pct, target.sell_pct))
    if not hit:
        return []
    target_pct = min(reached, sellable_pct)
    qty = position.qty * target_pct / HUNDRED - position.sold_qty
    if qty <= ZERO:
        return []
    top = hit[-1][0]
    return [
        SellOrder(
            collection=position.collection,
            token_id=position.token_id,
            qty=min(qty, position.open_qty),
            price=price,
            gain_pct=gain,
            reason=f"цель +{top}% — продаём {target_pct}% позиции, {ladder.hold_pct}% держим",
        )
    ]


@dataclass(frozen=True)
class IlliquidFlag:
    """Флаг неликвида: возраст позиции и правило снижения цены (История 80)."""

    collection: str
    token_id: str | None
    age_days: Decimal
    days_without_sales: Decimal | None
    current_price: Decimal
    suggested_price: Decimal
    markdown_pct: Decimal
    steps: int
    floor_price: Decimal
    reason: str

    @property
    def at_floor(self) -> bool:
        return self.suggested_price <= self.floor_price


def illiquid_flag(
    position: NftPosition,
    *,
    now: datetime | None = None,
    last_sale_at: datetime | None = None,
    config: NftConfig | IlliquidConfig | None = None,
) -> IlliquidFlag | None:
    """Неликвид — если позиция старше порога И по коллекции давно нет продаж."""
    cfg = config.illiquid if isinstance(config, NftConfig) else (config or load_nft().illiquid)
    at = now or datetime.now(UTC)
    age = position.age_days(at)
    if age < cfg.age_days:
        return None
    days_idle: Decimal | None = None
    if last_sale_at is not None:
        last = last_sale_at if last_sale_at.tzinfo else last_sale_at.replace(tzinfo=UTC)
        days_idle = Decimal((at - last).total_seconds()) / Decimal(86400)
        if days_idle < cfg.no_sales_days:
            return None
    listed_since = position.listed_at or position.opened_at
    if listed_since.tzinfo is None:
        listed_since = listed_since.replace(tzinfo=UTC)
    idle_days = Decimal((at - listed_since).total_seconds()) / Decimal(86400)
    steps = 0
    if cfg.markdown_every_days > 0:
        steps = int(idle_days / Decimal(cfg.markdown_every_days))
    current = position.listed_price or position.entry_price
    floor_price = position.entry_price * cfg.min_price_ratio
    factor = (HUNDRED - cfg.markdown_pct) / HUNDRED
    suggested = current * (factor**steps) if steps > 0 else current
    suggested = max(suggested, floor_price)
    return IlliquidFlag(
        collection=position.collection,
        token_id=position.token_id,
        age_days=age,
        days_without_sales=days_idle,
        current_price=current,
        suggested_price=suggested,
        markdown_pct=cfg.markdown_pct,
        steps=steps,
        floor_price=floor_price,
        reason=(
            f"позиция {int(age)} дн. без выхода"
            + (f", продаж по коллекции нет {int(days_idle)} дн." if days_idle is not None else "")
            + f" — цену снижаем на {cfg.markdown_pct}% каждые {cfg.markdown_every_days} дн."
        ),
    )


def markdown_price(
    price: Decimal, *, steps: int, config: NftConfig | IlliquidConfig | None = None
) -> Decimal:
    cfg = config.illiquid if isinstance(config, NftConfig) else (config or load_nft().illiquid)
    factor = (HUNDRED - cfg.markdown_pct) / HUNDRED
    return price * (factor ** max(0, steps))


def next_markdown_at(
    position: NftPosition,
    *,
    now: datetime | None = None,
    config: NftConfig | IlliquidConfig | None = None,
) -> datetime:
    cfg = config.illiquid if isinstance(config, NftConfig) else (config or load_nft().illiquid)
    base = position.listed_at or position.opened_at
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    at = now or datetime.now(UTC)
    step = timedelta(days=cfg.markdown_every_days or 1)
    nxt = base + step
    while nxt <= at:
        nxt += step
    return nxt
