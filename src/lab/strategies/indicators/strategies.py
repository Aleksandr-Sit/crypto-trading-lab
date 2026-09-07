"""Стратегии на индикаторах Pifagor/Coinmetrika — по карточкам тикета 13 (G08):
`cex-spot-pifagor-mfi-v0`, `cex-spot-pifagor-forever-sma-v0`, `cex-spot-coinmetrika-rsi4w-v0`,
`cex-spot-coinmetrika-mtf-oversold`, `cex-spot-coinmetrika-monthly-trend-flip`.

Общая механика: стратегия копит бары, считает индикатор по `Indicator.compute(frame)` и читает
последнюю строку: `signal_buy` без позиции — покупка, `signal_sell` или стоп с позицией — продажа.
Исполнение — рыночно на открытии следующего бара. Недельные/месячные карточки идут по дневным
барам (`timeframe: 1d` в манифесте), старшие периоды строит индикатор из закрытых периодов.
`indicator_version` в params — v0/v1: v1 подставляется через `INDICATOR_FACTORIES`.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from lab.contracts import Candle, Signal
from lab.strategies.base import Strategy
from lab.strategies.indicators import coinmetrika, pifagor
from lab.strategies.indicators.base import BaseIndicator
from lab.strategies.indicators.frame import Frame, period_key
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
MAX_HISTORY = 4000


class IndicatorStrategy(Strategy):
    """База: один индикатор, одна позиция на инструмент, стоп в процентах от входа."""

    indicator_name = ""
    max_history = MAX_HISTORY
    signal_period: str | None = (
        None  # 1w | 1M — индикатор пересчитывается на первом баре нового периода
    )

    def make_indicator(self) -> BaseIndicator:
        raise NotImplementedError

    def reset(self) -> None:
        self.history: list[Candle] = []
        self.qty = D(0)
        self.entry = D(0)
        self.indicator = self.make_indicator()
        self.min_bars = int(self.param("min_bars", 30))
        self.last_key: tuple | None = None

    def _size(self, price: Decimal) -> Decimal:
        capital = D(str(self.param("capital_usd", 10_000)))
        pct = D(str(self.param("position_pct_of_branch", 50))) / 100
        return capital * pct / price

    def on_bar(self, bar: Candle) -> list[Signal]:
        self.history.append(bar)
        del self.history[: -self.max_history]
        if len(self.history) < self.min_bars:
            return []
        buy = sell = False
        frame: Frame | None = None
        key = period_key(bar.ts, self.signal_period) if self.signal_period else None
        if key is None or key != self.last_key:  # закрылся период — считаем индикатор
            self.last_key = key
            frame = self.indicator.compute(Frame.from_candles(self.history))
            buy = bool(frame.last("signal_buy")) if "signal_buy" in frame else False
            sell = bool(frame.last("signal_sell")) if "signal_sell" in frame else False
        out: list[Signal] = []
        if self.qty > 0:
            stop_pct = self.param("stop_loss_pct")
            stopped = bool(stop_pct) and bar.close <= self.entry * (1 - D(str(stop_pct)) / 100)
            if sell or stopped:
                out.append(
                    self.signal(
                        bar,
                        "sell",
                        self.qty,
                        inputs={
                            "reason": "stop" if stopped and not sell else "signal_sell",
                            "indicator": self.indicator.name,
                            "version": self.indicator.version,
                        },
                    )
                )
                self.qty = D(0)
        elif buy:
            qty = self._size(bar.close)
            out.append(
                self.signal(
                    bar,
                    "buy",
                    qty,
                    inputs={
                        "indicator": self.indicator.name,
                        "version": self.indicator.version,
                        "value": _last_values(frame) if frame else {},
                    },
                )
            )
            self.qty, self.entry = qty, bar.close
        return out


def _last_values(frame: Frame) -> dict[str, Any]:
    return {
        k: (None if v != v else round(float(v), 6))
        for k, v in ((k, frame.last(k)) for k in frame.columns if k not in Frame.OHLCV_SET)
    }


Frame.OHLCV_SET = frozenset(("open", "high", "low", "close", "volume"))  # type: ignore[attr-defined]

# v1 подставляется сюда: фабрика по (indicator_name, version) -> индикатор
INDICATOR_FACTORIES: dict[tuple[str, str], Callable[[dict[str, Any]], BaseIndicator]] = {}


def _make(name: str, params: dict[str, Any], default: Callable[[], BaseIndicator]) -> BaseIndicator:
    version = str(params.get("indicator_version", "v0"))
    factory = INDICATOR_FACTORIES.get((name, version))
    return factory(params) if factory else default()


@preset(manifest_from_card(CARDS_DIR / "cex-spot-pifagor-mfi-v0.md"))
class PifagorMfiStrategy(IndicatorStrategy):
    card = "cex-spot-pifagor-mfi-v0"

    def make_indicator(self) -> BaseIndicator:
        p = self.params
        return _make(
            "mfi",
            p,
            lambda: pifagor.PifagorMfi(
                int(p.get("mfi_period", 14)),
                float(p.get("oversold", 20)),
                float(p.get("overbought", 80)),
            ),
        )


@preset(manifest_from_card(CARDS_DIR / "cex-spot-pifagor-forever-sma-v0.md"))
class ForeverSmaStrategy(IndicatorStrategy):
    card = "cex-spot-pifagor-forever-sma-v0"

    def make_indicator(self) -> BaseIndicator:
        p = self.params
        period = int(p.get("sma_period_days", 200))
        if p.get("sma_variant") == "weekly":
            period *= 7
        return _make(
            "forever-sma",
            p,
            lambda: pifagor.ForeverSma(
                period, float(p.get("entry_buffer_pct", 1.0)), float(p.get("exit_buffer_pct", 1.0))
            ),
        )


@preset(manifest_from_card(CARDS_DIR / "cex-spot-coinmetrika-rsi4w-v0.md"))
class Rsi4wStrategy(IndicatorStrategy):
    card = "cex-spot-coinmetrika-rsi4w-v0"
    signal_period = "1w"

    def make_indicator(self) -> BaseIndicator:
        p = self.params
        return _make(
            "rsi4w",
            p,
            lambda: coinmetrika.Rsi4w(
                int(p.get("rsi_period", 14)),
                int(p.get("smooth_weeks", 4)),
                float(p.get("oversold", 40)),
                float(p.get("overbought", 70)),
                int(p.get("ma_filter_days", 0) or 0),
                source_tf="1d",
            ),
        )


@preset(manifest_from_card(CARDS_DIR / "cex-spot-coinmetrika-mtf-oversold.md"))
class MtfOversoldStrategy(IndicatorStrategy):
    card = "cex-spot-coinmetrika-mtf-oversold"
    signal_period = "1w"

    def make_indicator(self) -> BaseIndicator:
        p = self.params
        return _make(
            "mtf-oversold",
            p,
            lambda: coinmetrika.MtfOversold(
                str(p.get("oscillator", "stoch_rsi")),
                int(p.get("rsi_period", 14)),
                int(p.get("stoch_period", 14)),
                int(p.get("k_smooth", 3)),
                int(p.get("d_smooth", 3)),
                float(p.get("oversold", 20)),
                float(p.get("overbought", 80)),
                tuple(p.get("confirm_timeframes", ["1w", "1M"])),
                int(p.get("max_bars_after_dot", 8)),
            ),
        )

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.param("entry_mode") == "in_zone":  # вход сразу на зелёной точке
            self.indicator.max_bars_after_dot = 0
        return super().on_bar(bar)


@preset(manifest_from_card(CARDS_DIR / "cex-spot-coinmetrika-monthly-trend-flip.md"))
class MonthlyTrendFlipStrategy(IndicatorStrategy):
    card = "cex-spot-coinmetrika-monthly-trend-flip"
    signal_period = "1M"

    def make_indicator(self) -> BaseIndicator:
        p = self.params
        return _make(
            "monthly-trend-flip",
            p,
            lambda: coinmetrika.MonthlyTrendFlip(
                str(p.get("trend_model", "supertrend")),
                int(p.get("supertrend_atr_period", 10)),
                float(p.get("supertrend_mult", 3.0)),
                int(p.get("ema_fast", 3)),
                int(p.get("ema_slow", 10)),
                int(p.get("rsi_period", 14)),
                int(p.get("rsi_fast_smooth", 3)),
                int(p.get("rsi_slow_smooth", 8)),
                float(p.get("oversold", 20)),
                float(p.get("overbought", 80)),
                int(p.get("rsi_confirm_window_months", 3)),
                str(p.get("exit_mode", "trend_flip")),
            ),
        )


__all__ = [
    "INDICATOR_FACTORIES",
    "ForeverSmaStrategy",
    "IndicatorStrategy",
    "MonthlyTrendFlipStrategy",
    "MtfOversoldStrategy",
    "PifagorMfiStrategy",
    "Rsi4wStrategy",
]
