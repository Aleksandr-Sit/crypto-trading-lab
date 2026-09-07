"""Стратегии ветки `nft`: ранняя вторичка и варианты минта.

`can_backtest=False` у всех: ни минт, ни первые минуты листинга не восстанавливаются из
истории — выжившие коллекции в неё попадают, а исчезнувшие из неё пропадают. Меряется
только форвардом, решение пишется в журнал до исхода.
"""

from lab.strategies.nft.mint import (
    MINT_EVENTS,
    MINT_VARIANTS,
    NftMintStrategy,
    latency_metrics,
    make_mint_strategy,
    mint_strategies,
    mint_variants,
)
from lab.strategies.nft.secondary import (
    ENTRY_EVENTS,
    NFT_FLOOR_EVENT,
    NFT_LISTING_EVENT,
    NFT_MINT_OPEN_EVENT,
    NftSecondaryStrategy,
    make_secondary_strategy,
    nft_manifest,
    nft_stop,
)


def nft_strategies(*, market: str = "magiceden", collection: str = "") -> list:
    """Что worker ставит на форвард по ветке: вторичка плюс все варианты минта."""
    return [make_secondary_strategy(collection, market=market), *mint_strategies(market=market)]


__all__ = [
    "ENTRY_EVENTS",
    "MINT_EVENTS",
    "MINT_VARIANTS",
    "NFT_FLOOR_EVENT",
    "NFT_LISTING_EVENT",
    "NFT_MINT_OPEN_EVENT",
    "NftMintStrategy",
    "NftSecondaryStrategy",
    "latency_metrics",
    "make_mint_strategy",
    "make_secondary_strategy",
    "mint_strategies",
    "mint_variants",
    "nft_manifest",
    "nft_stop",
    "nft_strategies",
]
