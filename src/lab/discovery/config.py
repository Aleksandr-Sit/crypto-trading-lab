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
    # Лидерборд отдаётся как есть, без сортировки по прибыли: 21.09.2026 в первой двадцатке
    # приехал лидер с −3.58 млн за месяц и ушёл карточкой в телеграм. Копировать убыточного
    # смысла нет, поэтому порог стоит здесь, а не в голове оператора.
    min_pnl_month_usd: Decimal = Decimal(0)


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
    # Предел на прогон — как у всех остальных лент. Без него ответ провайдера едет в очередь
    # целиком: 21.09.2026 запрос Dune завёл 4957 кандидатов за одно утро, и очередь
    # перестала быть читаемой.
    limit: int = 20
    # Кошелёк без известной прибыли — это просто адрес: ни отобрать, ни отранжировать.
    # Тот же прогон показал, чем это кончается: у всех 4957 кошельков `pnl_usd` был пуст,
    # потому что запрос отдавал справочник адресов бирж, а не рейтинг трейдеров.
    require_pnl: bool = True
    min_pnl_usd: Decimal = Decimal(0)


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
    # Окно по таймфрейму стратегии. Порог требует 30 сделок, а часовые и дневные правила
    # столько за квартал не набирают физически: кэш-энд-керри делает 48 сделок за пять
    # с половиной лет, то есть на 90 сутках у него две-три и вечный `insufficient`.
    # Без этой таблицы медленная стратегия не может пройти лестницу в принципе — не потому
    # что плоха, а потому что её не успевают измерить.
    window_days_by_tf: dict[str, int] = Field(
        default_factory=lambda: {"1d": 1825, "1w": 1825, "4h": 1095, "1h": 730}
    )
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
    # Сколько кандидат лежит нерешённым, прежде чем уйдёт в отклонённые сам. Решения по
    # кандидату система не принимает никогда, а очередь копится каждую неделю: без срока
    # годности в ней к 21.09.2026 лежало 5075 записей, и ни по одной не было решения.
    # 0 — срок не применяется вовсе.
    candidate_expire_days: int = 30
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
