"""Coin Metrika (Вадим) — порты индикаторов.

[ИНДИКАТОР v0 — по публичным описаниям; v1 — по скрипту пользователя]

Источники: посты канала https://t.me/s/coinmetrika («кастомный 4-недельный индикатор», «зеленка»,
«пользовательский RSI», 365-дневная MA), сайт https://coinmetrika.capital/ (закрытая подписка,
Pine-кода нет) и три скриншота пользователя (`.autopilot/.../user-inputs/coinmetrika-*.png`),
разобранные в `docs/research/indicators/coinmetrika.md`: названия 8 индикаторов, дефолты
`40 80 135 10 18 31` (Halving Cycle Profit), `70 SMA 14 1` (M2 Global), `Slow` (ALTs Cloud),
шкалы 0–100 с зонами 80/20, даты сигналов на BTC 1W/1M.

Что кодируется (гипотезы формы, не факты): `Rsi4w`, `MtfOversold`, `MonthlyTrendFlip`,
`CycleMomentum`; остальные — заглушки `[ИНДИКАТОР — нужен скрипт]`.
Все три «старших» индикатора считаются из **дневных** свечей: недели/месяцы строятся агрегацией,
на дневную сетку возвращается значение последнего закрытого периода — без заглядывания в будущее.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from lab.strategies.indicators.base import LABEL_V0, BaseIndicator, StubIndicator
from lab.strategies.indicators.frame import Frame
from lab.strategies.indicators.ta import cross_down, cross_up, ema, rsi, sma, stoch_rsi, supertrend

CHANNEL = "https://t.me/s/coinmetrika"
SCREENSHOTS = ".autopilot/2026-09-05-crypto-trading-lab--wip/user-inputs/"


class Rsi4w(BaseIndicator):
    """«4-недельный индикатор / зеленка» — гипотеза: RSI(14) на недельных свечах, сглаженный SMA(4);
    «зеленка» = выход сглаженного RSI из зоны oversold снизу; фильтр `close > SMA(ma_filter_days)`.
    На недельном фрейме считает напрямую; на дневном — агрегирует недели (закрытые).
    Колонки: `rsi4w`, `ma_filter` (1/0/nan), `signal_buy`, `signal_sell`."""

    name: ClassVar[str] = "rsi4w"
    source: ClassVar[str] = CHANNEL
    label: ClassVar[str] = LABEL_V0

    def __init__(
        self,
        rsi_period: int = 14,
        smooth: int = 4,
        oversold: float = 40,
        overbought: float = 70,
        ma_filter_days: int = 0,
        source_tf: str = "1w",
    ) -> None:
        self.rsi_period, self.smooth = rsi_period, smooth
        self.oversold, self.overbought = oversold, overbought
        self.ma_filter_days, self.source_tf = ma_filter_days, source_tf

    def compute(self, frame: Frame) -> Frame:
        if self.source_tf == "1d":
            weekly = frame.resample("1w")
            r = sma(rsi(weekly["close"], self.rsi_period), self.smooth)
            w = weekly.with_columns(r=r)
            value = frame.expand_from(w, "r", "1w")
        else:
            value = sma(rsi(frame["close"], self.rsi_period), self.smooth)
        ma_ok = np.full(len(frame), 1.0)
        if self.ma_filter_days:
            m = sma(frame["close"], self.ma_filter_days)
            ma_ok = np.where(np.isnan(m), np.nan, (frame["close"] > m).astype(float))
        buy = cross_up(value, self.oversold) * np.nan_to_num(ma_ok)
        return frame.with_columns(
            rsi4w=value,
            ma_filter=ma_ok,
            signal_buy=buy,
            signal_sell=cross_up(value, self.overbought),
        )


class MtfOversold(BaseIndicator):
    """Скриншот 1 (BTC 1W): осциллятор 0–100, две линии, зоны 80/20, «крупная точка» = одно и то же
    условие сразу на нескольких таймфреймах. Гипотеза: Stochastic RSI (14, 14, 3, 3) на 1W и 1M;
    зелёная точка — K < oversold на обоих, красная — K > overbought на обоих.
    Вход в дневном фрейме. Колонки: `osc_1w`, `osc_1m`, `signal_dot_green`, `signal_dot_red`,
    `signal_buy` (1W-линия пересекает oversold вверх после зелёной точки), `signal_sell`."""

    name: ClassVar[str] = "mtf-oversold"
    source: ClassVar[str] = SCREENSHOTS + "coinmetrika-1-btc-1w-momentum.png"
    label: ClassVar[str] = LABEL_V0

    def __init__(
        self,
        oscillator: str = "stoch_rsi",
        rsi_period: int = 14,
        stoch_period: int = 14,
        k_smooth: int = 3,
        d_smooth: int = 3,
        oversold: float = 20,
        overbought: float = 80,
        timeframes: tuple[str, ...] = ("1w", "1M"),
        max_bars_after_dot: int = 8,
    ) -> None:
        self.oscillator = oscillator
        self.rsi_period, self.stoch_period = rsi_period, stoch_period
        self.k_smooth, self.d_smooth = k_smooth, d_smooth
        self.oversold, self.overbought = oversold, overbought
        self.timeframes, self.max_bars_after_dot = tuple(timeframes), max_bars_after_dot

    def _osc(self, close: np.ndarray) -> np.ndarray:
        if self.oscillator == "rsi_smoothed":
            return ema(rsi(close, self.rsi_period), self.k_smooth)
        k, _d = stoch_rsi(close, self.rsi_period, self.stoch_period, self.k_smooth, self.d_smooth)
        return k

    def compute(self, frame: Frame) -> Frame:
        cols: dict[str, np.ndarray] = {}
        for tf in self.timeframes:
            hi = frame.resample(tf)
            cols[f"osc_{tf.lower()}"] = frame.expand_from(
                hi.with_columns(o=self._osc(hi["close"])), "o", tf
            )
        stack = np.vstack(list(cols.values()))
        known = ~np.isnan(stack).any(axis=0)
        green = known & (stack < self.oversold).all(axis=0)
        red = known & (stack > self.overbought).all(axis=0)
        primary = cols[f"osc_{self.timeframes[0].lower()}"]
        up = cross_up(primary, self.oversold).astype(bool)
        # покупка: 1W-линия выходит из зоны не позже max_bars_after_dot недель после зелёной точки
        buy = np.zeros(len(frame))
        last_dot = None
        for i in range(len(frame)):
            if green[i]:
                last_dot = i
            if up[i] and last_dot is not None and (i - last_dot) <= self.max_bars_after_dot * 7:
                buy[i] = 1.0
                last_dot = None
        return frame.with_columns(
            **cols,
            signal_dot_green=green.astype(float),
            signal_dot_red=red.astype(float),
            signal_buy=buy,
            signal_sell=red.astype(float),
        )


class MonthlyTrendFlip(BaseIndicator):
    """Скриншот 2 (BTC 1M): «индикатор тренда» красит фон и ставит BUY/SELL на разворотах,
    «пользовательский RSI» — две сглаженные линии с зонами 80/20. Гипотеза: Supertrend(10, 3)
    (или EMA(3)/EMA(10)) на месячных свечах + EMA(3)/EMA(8) от RSI(14). BUY = флип тренда вверх,
    подтверждённый выходом быстрого RSI из зоны oversold в окне ±window месяцев.
    Вход — дневной фрейм. Колонки: `trend` (+1/−1), `rsi_fast`, `rsi_slow`, `signal_buy`, `signal_sell`."""

    name: ClassVar[str] = "monthly-trend-flip"
    source: ClassVar[str] = SCREENSHOTS + "coinmetrika-2-btc-1m-trend-rsi.png"
    label: ClassVar[str] = LABEL_V0

    def __init__(
        self,
        trend_model: str = "supertrend",
        atr_period: int = 10,
        mult: float = 3.0,
        ema_fast: int = 3,
        ema_slow: int = 10,
        rsi_period: int = 14,
        rsi_fast_smooth: int = 3,
        rsi_slow_smooth: int = 8,
        oversold: float = 20,
        overbought: float = 80,
        confirm_window_months: int = 3,
        exit_mode: str = "trend_flip",
    ) -> None:
        self.trend_model, self.atr_period, self.mult = trend_model, atr_period, mult
        self.ema_fast, self.ema_slow, self.rsi_period = ema_fast, ema_slow, rsi_period
        self.rsi_fast_smooth, self.rsi_slow_smooth = rsi_fast_smooth, rsi_slow_smooth
        self.oversold, self.overbought = oversold, overbought
        self.window, self.exit_mode = confirm_window_months, exit_mode

    def monthly(self, frame: Frame) -> Frame:
        m = frame.resample("1M")
        if self.trend_model == "ema_cross":
            trend = np.sign(ema(m["close"], self.ema_fast) - ema(m["close"], self.ema_slow))
        else:
            _line, trend = supertrend(m["high"], m["low"], m["close"], self.atr_period, self.mult)
        r = rsi(m["close"], self.rsi_period)
        fast, slow = ema(r, self.rsi_fast_smooth), ema(r, self.rsi_slow_smooth)
        flip_up = cross_up(np.nan_to_num(trend), 0.0)
        flip_down = cross_down(np.nan_to_num(trend), 0.0)
        rsi_up = cross_up(fast, self.oversold)
        rsi_down = cross_down(fast, self.overbought)
        buy = np.zeros(len(m))
        w = self.window
        for i in range(len(m)):
            lo, hi = max(0, i - w), min(len(m), i + w + 1)
            if flip_up[i] and rsi_up[lo:hi].any():
                buy[i] = 1.0
            elif rsi_up[i] and flip_up[lo:i].any():
                buy[i] = 1.0
        sell = flip_down if self.exit_mode == "trend_flip" else rsi_down
        return m.with_columns(
            trend=trend, rsi_fast=fast, rsi_slow=slow, signal_buy=buy, signal_sell=sell
        )

    def compute(self, frame: Frame) -> Frame:
        m = self.monthly(frame)
        cols = {
            name: frame.expand_from(m, name, "1M") for name in ("trend", "rsi_fast", "rsi_slow")
        }
        # сигнал месяца становится известен в первый день следующего месяца — ставим его там один раз
        for name in ("signal_buy", "signal_sell"):
            expanded = np.nan_to_num(frame.expand_from(m, name, "1M"))
            first = np.zeros(len(frame))
            for i in range(len(frame)):
                if expanded[i] and (i == 0 or _month(frame.ts[i]) != _month(frame.ts[i - 1])):
                    first[i] = 1.0
            cols[name] = first
        return frame.with_columns(**cols)


def _month(t) -> tuple[int, int]:
    return (t.year, t.month)


class CycleMomentum(BaseIndicator):
    """Скриншот 3: «Cycle Momentum CoinMetrika» — гистограмма, шкала −25…125, уровни 0/25/75/100.
    Гипотеза формы: (RSI(period) − 50)·2.5 — шкала −125…125, зелёные столбцы > 0. Формула неизвестна."""

    name: ClassVar[str] = "cycle-momentum"
    source: ClassVar[str] = SCREENSHOTS + "coinmetrika-3-tradingview-indicators-list.png"
    label: ClassVar[str] = LABEL_V0

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, frame: Frame) -> Frame:
        return frame.with_columns(cycle_momentum=(rsi(frame["close"], self.period) - 50.0) * 2.5)


def _stub(cls_name: str, name: str, known: str) -> type[StubIndicator]:
    return type(
        cls_name, (StubIndicator,), {"name": name, "source": SCREENSHOTS, "what_is_known": known}
    )


SafetyLines = _stub(
    "SafetyLines",
    "safety-lines",
    "«Safety Lines by CoinMetrika Trade Waves», параметры не показаны",
)
FiboWaveBands = _stub(
    "FiboWaveBands", "fibowave-bands", "полосы по фибо-уровням; от каких экстремумов — неизвестно"
)
MaPack = _stub("MaPack", "ma-pack", "набор скользящих средних; периоды и типы неизвестны")
HalvingCycleProfit = _stub(
    "HalvingCycleProfit", "halving-cycle-profit", "дефолты 40 80 135 10 18 31, смысл неизвестен"
)
M2Global = _stub(
    "M2Global", "m2-global", "дефолты 70 SMA 14 1; нужен ряд глобальной M2 (нет источника)"
)
TrendDip = _stub("TrendDip", "trend-dip", "«покупка просадки в тренде»; формулы нет")
AltsCloud = _stub("AltsCloud", "alts-cloud", "режим Slow; формула облака неизвестна")

INDICATORS: dict[str, type[BaseIndicator]] = {
    c.name: c
    for c in (
        Rsi4w,
        MtfOversold,
        MonthlyTrendFlip,
        CycleMomentum,
        SafetyLines,
        FiboWaveBands,
        MaPack,
        HalvingCycleProfit,
        M2Global,
        TrendDip,
        AltsCloud,
    )
}


def by_name(name: str, **kwargs) -> BaseIndicator:
    return INDICATORS[name](**kwargs)


__all__ = [
    "INDICATORS",
    "CycleMomentum",
    "M2Global",
    "MonthlyTrendFlip",
    "MtfOversold",
    "Rsi4w",
    "by_name",
]
