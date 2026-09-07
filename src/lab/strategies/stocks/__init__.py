"""Сигналы по акциям (G01.2, История 85a): те же стратегии с правилами на дневных свечах акций,
`asset_class: stock`, ветка `rh`, площадка `stocks` — сигнал без исполнения
(ступень `signal` навсегда,
исполнение оператором вручную, отметка кнопкой в боте)."""

from __future__ import annotations

from typing import Any

from lab.contracts import Branch, Candle, Signal, StrategyManifest
from lab.strategies import registry
from lab.strategies.base import Strategy

STOCK_VENUE = "stocks"
SIGNAL_META = {"asset_class": "stock", "execution": "manual", "rung_cap": "signal"}


def is_signal_only(manifest: StrategyManifest) -> bool:
    return manifest.params.get("asset_class") == "stock"


def stock_manifest(
    base: StrategyManifest,
    tickers: list[str],
    *,
    params: dict[str, Any] | None = None,
    timeframe: str = "1d",
) -> StrategyManifest:
    return base.model_copy(
        update={
            "branch": Branch.RH,
            "venue": STOCK_VENUE,
            "instruments": list(tickers),
            "timeframe": timeframe,
            "params": {
                **base.params,
                **(params or {}),
                "asset_class": "stock",
                "signal_only": True,
            },
            "description": (base.description + " · акции: только сигнал").strip(" ·"),
        }
    )


class StockSignalStrategy(Strategy):
    """Обёртка: правило — внутренняя стратегия, каждый сигнал помечен `asset_class: stock`,
    `execution: manual` — исполнитель для него не существует."""

    def __init__(self, inner: Strategy) -> None:
        self.inner = inner
        super().__init__(inner.manifest)

    def reset(self) -> None:
        if hasattr(self, "inner"):
            self.inner.reset()

    def _tag(self, signals: list[Signal]) -> list[Signal]:
        return [s.model_copy(update={"meta": {**s.meta, **SIGNAL_META}}) for s in signals]

    def on_bar(self, bar: Candle) -> list[Signal]:
        return self._tag(self.inner.on_bar(bar))

    def on_event(self, event) -> list[Signal]:
        return self._tag(self.inner.on_event(event))


def make_stock_strategy(
    strategy_id: str,
    *,
    tickers: list[str],
    params: dict[str, Any] | None = None,
    timeframe: str = "1d",
) -> StockSignalStrategy:
    """Стратегия каталога на акциях: `cex-spot-indicator-x` → `rh-indicator-x`
    с `asset_class: stock`."""
    cls = registry.klass(strategy_id)
    manifest = stock_manifest(
        registry.manifest(strategy_id), tickers, params=params, timeframe=timeframe
    )
    return StockSignalStrategy(cls(manifest))


__all__ = [
    "SIGNAL_META",
    "STOCK_VENUE",
    "StockSignalStrategy",
    "is_signal_only",
    "make_stock_strategy",
    "stock_manifest",
]
