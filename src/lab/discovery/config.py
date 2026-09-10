"""`config/discovery.yaml` — источники поиска, окно переизмерения, правила перелива."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from lab.config import load_config

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "discovery.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool = True


class OkxLeadConfig(_Base):
    limit: int = 20
    inst_type: str = "SWAP"
    min_days: int = 0
    min_win_rate_pct: Decimal = Decimal(0)


class HyperliquidConfig(_Base):
    limit: int = 20
    manual: list[str] = Field(default_factory=list)


class PolymarketConfig(_Base):
    limit: int = 20
    window: str = "all"
    order_by: str = "pnl"


class ProviderConfig(_Base):
    id: str
    url: str = ""
    key_env: str = ""
    weight: int = 1
    wallet_field: str = "wallet"
    chain: str = ""


class SmartMoneyConfig(_Base):
    providers: list[ProviderConfig] = Field(default_factory=list)


class GithubConfig(_Base):
    limit: int = 10
    min_stars: int = 0
    branch: str = "cex-spot"
    queries: list[str] = Field(default_factory=list)


class NftLaunchpadConfig(_Base):
    limit: int = 20
    markets: list[str] = Field(default_factory=list)
    calendars: list[str] = Field(default_factory=list)


class SeedConfig(_Base):
    path: str = "candidates/seed.md"


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    okx_lead: OkxLeadConfig = OkxLeadConfig()
    hyperliquid: HyperliquidConfig = HyperliquidConfig()
    polymarket: PolymarketConfig = PolymarketConfig()
    smart_money: SmartMoneyConfig = SmartMoneyConfig()
    github: GithubConfig = GithubConfig()
    nft_launchpad: NftLaunchpadConfig = NftLaunchpadConfig()
    seed: SeedConfig = SeedConfig()


class RemeasureConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    window_days: int = 90
    valid_weeks: int = 4
    mode: str = "backtest"
    # Бюджет времени на весь воскресный прогон, минут. Сервер общий с боевым ботом
    # соседнего проекта; без предела ночь превращается в многочасовую нагрузку.
    budget_minutes: int = 60


class RebalanceConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    risky_branches: list[str] = Field(default_factory=lambda: ["meme", "nft", "prediction", "copy"])
    to_branch: str = "cex-spot"
    min_amount_usd: Decimal = Decimal(50)


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    sources: SourcesConfig = SourcesConfig()
    max_cards_per_scan: int = 10
    remeasure: RemeasureConfig = RemeasureConfig()
    rebalance: RebalanceConfig = RebalanceConfig()


def load_discovery(path: Path | str | None = None) -> DiscoveryConfig:
    return load_config(path or CONFIG_PATH, DiscoveryConfig)


__all__ = [
    "CONFIG_PATH",
    "DiscoveryConfig",
    "GithubConfig",
    "HyperliquidConfig",
    "NftLaunchpadConfig",
    "OkxLeadConfig",
    "PolymarketConfig",
    "RebalanceConfig",
    "RemeasureConfig",
    "SeedConfig",
    "SmartMoneyConfig",
    "SourcesConfig",
    "load_discovery",
]
