"""Издержки NFT-сделки по компонентам (История 79, R18.1).

Роялти и комиссию площадки платит продавец, газ — обе стороны. Поэтому «издержки покупки»
и «издержки продажи» — разные наборы, а не один усреднённый процент: иначе круг выглядит
дешевле, чем он есть, ровно на роялти.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.contracts import Costs
from lab.feeds.nft.config import NftConfig, load_nft

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class CostBreakdown:
    """Издержки по компонентам — то, что показывается в карточке сделки."""

    royalty: Decimal = Decimal(0)
    marketplace_fee: Decimal = Decimal(0)
    gas: Decimal = Decimal(0)
    priority_fee: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.royalty + self.marketplace_fee + self.gas + self.priority_fee

    def to_costs(self) -> Costs:
        return Costs(
            fee=self.marketplace_fee,
            gas=self.gas,
            royalty=self.royalty,
            priority_fee=self.priority_fee,
        )


def breakdown(
    price: Decimal,
    *,
    market: str = "",
    side: str = "buy",
    config: NftConfig | None = None,
    royalty_pct: Decimal | None = None,
    fee_pct: Decimal | None = None,
    gas_usd: Decimal | None = None,
    priority_fee: Decimal = Decimal(0),
) -> CostBreakdown:
    cfg = config or load_nft()
    default_royalty, default_fee, default_gas = cfg.costs.for_market(market)
    royalty = default_royalty if royalty_pct is None else royalty_pct
    fee = default_fee if fee_pct is None else fee_pct
    gas = default_gas if gas_usd is None else gas_usd
    if side == "buy":  # покупатель платит цену и газ; роялти и комиссия — на продавце
        return CostBreakdown(gas=gas, priority_fee=priority_fee)
    return CostBreakdown(
        royalty=price * royalty / HUNDRED,
        marketplace_fee=price * fee / HUNDRED,
        gas=gas,
        priority_fee=priority_fee,
    )


def nft_costs(
    price: Decimal,
    *,
    market: str = "",
    side: str = "buy",
    config: NftConfig | None = None,
    royalty_pct: Decimal | None = None,
    fee_pct: Decimal | None = None,
    gas_usd: Decimal | None = None,
    priority_fee: Decimal = Decimal(0),
) -> Costs:
    """`Costs` ядра из компонентов NFT-сделки."""
    return breakdown(
        price,
        market=market,
        side=side,
        config=config,
        royalty_pct=royalty_pct,
        fee_pct=fee_pct,
        gas_usd=gas_usd,
        priority_fee=priority_fee,
    ).to_costs()


def round_trip(
    entry: Decimal,
    exit_price: Decimal,
    *,
    market: str = "",
    config: NftConfig | None = None,
    **kw: Decimal | None,
) -> CostBreakdown:
    """Издержки круга: покупка + продажа. Роялти считается от цены выхода."""
    buy = breakdown(entry, market=market, side="buy", config=config, **kw)
    sell = breakdown(exit_price, market=market, side="sell", config=config, **kw)
    return CostBreakdown(
        royalty=buy.royalty + sell.royalty,
        marketplace_fee=buy.marketplace_fee + sell.marketplace_fee,
        gas=buy.gas + sell.gas,
        priority_fee=buy.priority_fee + sell.priority_fee,
    )
