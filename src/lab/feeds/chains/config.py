"""`config/chains.yaml`: какие сети есть, чем ходят, какие пороги у кошельков.

Включение сети — флагом `CHAINS_ENABLED` (G07: по одной, начиная с популярных).
Порядок задаёт конфиг, а не флаг: Solana → Ethereum/Base → BNB → TON.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from lab.config import CONFIG_DIR, ConfigError, load_config

CHAINS_ENV = "CHAINS_ENABLED"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChainSpec(_Cfg):
    feed_id: str
    networks: list[str] = Field(min_length=1)
    weight: int = 1
    quota_limit: int | None = None
    quota_period: str = "month"
    key_env: str | None = None
    note: str = ""


class WalletThresholds(_Cfg):
    """Пороги отбора и флагов накрутки (Истории 57–59, 63)."""

    min_trades: int = 30
    min_win_rate_pct: Decimal = Decimal(45)
    max_dd_pct: Decimal = Decimal(50)
    min_age_days: int = 30
    min_survived_pct: Decimal = Decimal(30)
    wash_round_trip_s: int = 300
    wash_share_pct: Decimal = Decimal(20)
    own_token_share_pct: Decimal = Decimal(50)
    single_luck_share_pct: Decimal = Decimal(60)
    lag_delays_s: list[int] = Field(default_factory=lambda: [5, 30, 120], min_length=1)
    leader_min_score_pct: Decimal = Decimal(0)


class ChainsConfig(_Cfg):
    order: list[str] = Field(min_length=1)
    chains: dict[str, ChainSpec]
    wallets: WalletThresholds = Field(default_factory=WalletThresholds)

    def spec(self, chain: str) -> ChainSpec:
        try:
            return self.chains[chain]
        except KeyError as err:
            raise ConfigError(f"chains.yaml: сеть {chain!r} не описана") from err


@lru_cache(maxsize=4)
def _load(path: str) -> ChainsConfig:
    config = load_config(path, ChainsConfig)
    unknown = [c for c in config.order if c not in config.chains]
    if unknown:
        raise ConfigError(f"chains.yaml: в order сети без описания: {', '.join(unknown)}")
    return config


def load_chains(path: Path | str | None = None) -> ChainsConfig:
    return _load(str(path or CONFIG_DIR / "chains.yaml"))


def enabled_chains(
    env: Mapping[str, str] | None = None, config: ChainsConfig | None = None
) -> list[str]:
    """Сети из `CHAINS_ENABLED` в порядке конфига. Неизвестное имя — `ConfigError`."""
    env = os.environ if env is None else env
    config = config or load_chains()
    raw = (env.get(CHAINS_ENV) or "").strip()
    if not raw:
        return []
    wanted = [name.strip().lower() for name in raw.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in config.chains]
    if unknown:
        known = ", ".join(config.order)
        raise ConfigError(f"{CHAINS_ENV}: неизвестные сети {', '.join(unknown)}; известны: {known}")
    return [name for name in config.order if name in wanted]


def wallet_thresholds(config: ChainsConfig | None = None) -> WalletThresholds:
    return (config or load_chains()).wallets


__all__ = [
    "CHAINS_ENV",
    "ChainSpec",
    "ChainsConfig",
    "WalletThresholds",
    "enabled_chains",
    "load_chains",
    "wallet_thresholds",
]
