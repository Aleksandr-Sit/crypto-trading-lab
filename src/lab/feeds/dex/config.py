"""Конфиг ветки `meme` — `config/meme.yaml` (фильтр потока, честность, свопы, лестница)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lab.config import ConfigError

DEFAULT_PATH = Path("config/meme.yaml")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StreamConfig(_Model):
    max_tracked: int = 500
    max_history_per_token: int = 200
    aggregate_window_min: int = 60
    min_liquidity_usd: Decimal = Decimal(3000)
    min_volume_usd: Decimal = Decimal(0)
    min_buys: int = 0
    max_age_min: int = 180


class HonestyConfig(_Model):
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top_holder_pct: Decimal = Decimal(25)
    max_top10_pct: Decimal = Decimal(60)
    min_liquidity_usd: Decimal = Decimal(5000)
    min_age_s: int = 60
    blocklist: list[str] = Field(default_factory=list)


class ExecutionConfig(_Model):
    max_slippage_pct: Decimal = Decimal(5)
    slippage_pct: Decimal = Decimal("1.5")
    priority_fee_usd: Decimal = Decimal("0.05")
    max_priority_fee_usd: Decimal = Decimal(1)
    retry_priority_multiplier: Decimal = Decimal(2)
    max_attempts: int = 3


class LadderTarget(_Model):
    gain_pct: Decimal
    sell_pct: Decimal


class LadderConfig(_Model):
    targets: list[LadderTarget] = Field(default_factory=list)
    trailing_pct: Decimal = Decimal(25)
    stop_loss_pct: Decimal = Decimal(50)


class MemeConfig(_Model):
    version: int = 1
    stream: StreamConfig = StreamConfig()
    honesty: HonestyConfig = HonestyConfig()
    execution: ExecutionConfig = ExecutionConfig()
    ladder: LadderConfig = LadderConfig()


_cache: dict[str, MemeConfig] = {}


def load_meme(path: str | Path | None = None) -> MemeConfig:
    """Конфиг ветки. Файла нет — значения по умолчанию (ветка работает и без него)."""
    target = Path(path) if path is not None else DEFAULT_PATH
    key = str(target)
    if key in _cache:
        return _cache[key]
    if not target.exists():
        config = MemeConfig()
    else:
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            config = MemeConfig(**raw)
        except (yaml.YAMLError, ValidationError, TypeError) as err:
            raise ConfigError(f"{target}: {err}") from err
    _cache[key] = config
    return config


__all__ = [
    "DEFAULT_PATH",
    "ExecutionConfig",
    "HonestyConfig",
    "LadderConfig",
    "LadderTarget",
    "MemeConfig",
    "StreamConfig",
    "load_meme",
]
