"""Конфиг ветки `nft` — `config/nft.yaml` (площадки, внимание, лестница, минт, издержки).

Веса индекса внимания живут здесь и помечены `hypothesis: true`: они сами предмет замера
(История 74a), а не установленная истина.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lab.config import ConfigError

DEFAULT_PATH = Path("config/nft.yaml")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketConfig(_Model):
    feed_id: str
    chain: str = ""
    quota_limit: int = 0
    quota_period: str = "minute"
    weight: int = 1
    key_env: str = ""
    key_ttl_days: int = 0
    enabled: bool = True
    read_only: bool = False


class AttentionWeights(_Model):
    mentions_growth: Decimal = Decimal("0.35")
    allowlist_demand: Decimal = Decimal("0.25")
    launchpad_fill: Decimal = Decimal("0.20")
    creator_score: Decimal = Decimal("0.20")

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "mentions_growth": self.mentions_growth,
            "allowlist_demand": self.allowlist_demand,
            "launchpad_fill": self.launchpad_fill,
            "creator_score": self.creator_score,
        }


class AttentionConfig(_Model):
    hypothesis: bool = True
    weights: AttentionWeights = AttentionWeights()
    mentions_window_h: int = 24
    min_score_to_watch: Decimal = Decimal("0.35")


class CreatorConfig(_Model):
    windows_days: list[int] = Field(default_factory=lambda: [1, 7, 30])
    success_floor_ratio: Decimal = Decimal(1)
    success_window_days: int = 7
    min_collections: int = 1
    half_life_days: int = 180


class LadderTarget(_Model):
    gain_pct: Decimal
    sell_pct: Decimal


class LadderConfig(_Model):
    targets: list[LadderTarget] = Field(default_factory=list)
    hold_pct: Decimal = Decimal(25)
    stop_loss_pct: Decimal = Decimal(40)


class IlliquidConfig(_Model):
    age_days: int = 14
    no_sales_days: int = 7
    markdown_pct: Decimal = Decimal(10)
    markdown_every_days: int = 3
    min_price_ratio: Decimal = Decimal("0.5")


class SecondaryConfig(_Model):
    window_min: int = 15
    max_floor_premium_pct: Decimal = Decimal(5)
    min_volume_usd: Decimal = Decimal(5000)
    min_sales_per_min: Decimal = Decimal(2)
    min_attention: Decimal = Decimal("0.4")
    decision_budget_ms: int = 2000


class MintVariant(_Model):
    slug: str
    note: str = ""
    priority_fee_usd: Decimal = Decimal(0)
    wallets: int = 1
    send_offset_s: int = 0
    allowlist_only: bool = False


class MintConfig(_Model):
    qty: int = 1
    max_price_usd: Decimal = Decimal(200)
    max_attempts: int = 3
    variants: list[MintVariant] = Field(default_factory=list)


class MarketCosts(_Model):
    marketplace_fee_pct: Decimal | None = None
    gas_usd: Decimal | None = None
    royalty_pct: Decimal | None = None


class CostsConfig(_Model):
    royalty_pct: Decimal = Decimal(5)
    marketplace_fee_pct: Decimal = Decimal(2)
    gas_usd: Decimal = Decimal(3)
    per_market: dict[str, MarketCosts] = Field(default_factory=dict)

    def for_market(self, market: str) -> tuple[Decimal, Decimal, Decimal]:
        """(роялти %, комиссия площадки %, газ USD) — тариф площадки поверх общего."""
        override = self.per_market.get(market)
        royalty = self.royalty_pct
        fee = self.marketplace_fee_pct
        gas = self.gas_usd
        if override is not None:
            royalty = override.royalty_pct if override.royalty_pct is not None else royalty
            fee = (
                override.marketplace_fee_pct
                if override.marketplace_fee_pct is not None
                else fee
            )
            gas = override.gas_usd if override.gas_usd is not None else gas
        return royalty, fee, gas


class CalendarConfig(_Model):
    id: str
    url: str = ""
    enabled: bool = True


class NftConfig(_Model):
    version: int = 1
    markets: dict[str, MarketConfig] = Field(default_factory=dict)
    attention: AttentionConfig = AttentionConfig()
    creator: CreatorConfig = CreatorConfig()
    ladder: LadderConfig = LadderConfig()
    illiquid: IlliquidConfig = IlliquidConfig()
    secondary: SecondaryConfig = SecondaryConfig()
    mint: MintConfig = MintConfig()
    costs: CostsConfig = CostsConfig()
    calendars: list[CalendarConfig] = Field(default_factory=list)

    def market(self, name: str) -> MarketConfig:
        try:
            return self.markets[name]
        except KeyError as exc:  # pragma: no cover — защита от опечатки в коде
            raise ConfigError(f"nft.yaml: площадка {name!r} не описана") from exc

    def variant(self, slug: str) -> MintVariant:
        for variant in self.mint.variants:
            if variant.slug == slug:
                return variant
        raise ConfigError(f"nft.yaml: вариант минта {slug!r} не описан")


_CACHE: dict[str, NftConfig] = {}


def load_nft(path: str | Path | None = None, *, cache: bool = True) -> NftConfig:
    file = Path(path or DEFAULT_PATH)
    key = str(file)
    if cache and key in _CACHE:
        return _CACHE[key]
    if not file.exists():
        raise ConfigError(f"конфиг NFT не найден: {file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        config = NftConfig(**raw)
    except (yaml.YAMLError, ValidationError, TypeError) as exc:
        raise ConfigError(f"конфиг NFT {file} не читается: {exc}") from exc
    if cache:
        _CACHE[key] = config
    return config
