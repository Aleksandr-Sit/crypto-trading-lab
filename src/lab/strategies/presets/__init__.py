"""Пресеты чужих ботов как стратегии-гипотезы (R07). Регистрируются при импорте."""

from lab.strategies.presets.bots import (
    DcaSafetyStrategy,
    FuturesGridNeutralStrategy,
    MartingaleCappedStrategy,
    SpotGridStrategy,
    TrailingBreakoutStrategy,
)

__all__ = [
    "DcaSafetyStrategy",
    "FuturesGridNeutralStrategy",
    "MartingaleCappedStrategy",
    "SpotGridStrategy",
    "TrailingBreakoutStrategy",
]
